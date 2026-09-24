"""Automação diária contra o banco real.

A idempotência aqui é provada pelo **banco**, não pela memória do processo: a
segunda execução tenta inserir a mesma `dedupe_key` e o índice único recusa.
É a diferença entre um teste que prova a intenção e um que prova a garantia.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def contrato_a_vencer(api: TestClient, cabecalho_operador, cliente_criado):  # type: ignore[no-untyped-def]
    """Contrato que vence daqui a exatamente 30 dias — um limiar de alerta.

    A vigência **começa hoje**, de propósito: um contrato iniciado há um ano
    teria onze parcelas já vencidas, e o job geraria onze cobranças de atraso
    além do alerta de vencimento. O teste é sobre o alerta, então o cenário é
    montado para conter só ele — cobrança em atraso tem os testes dela.
    """
    hoje = date.today()
    resposta = api.post(
        "/api/v1/contracts",
        headers=cabecalho_operador,
        json={
            "client_id": cliente_criado["id"],
            "number": "CT-VENCE-30",
            "monthly_amount": "980.00",
            "start_date": hoje.isoformat(),
            "end_date": (hoje + timedelta(days=30)).isoformat(),
            "due_day": 10,
        },
    )
    assert resposta.status_code == 201, resposta.text
    return resposta.json()


class TestExecucao:
    def test_gera_alerta_marca_status_e_envia(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer, notifier
    ) -> None:
        resultado = api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})
        assert resultado.status_code == 200

        dados = resultado.json()
        assert dados["status"] == "SUCCESS"
        assert dados["alertas_gerados"] == 1
        assert dados["envio"]["enviadas"] == 1
        assert len(notifier.enviadas) == 1

        contrato = api.get(
            f"/api/v1/contracts/{contrato_a_vencer['id']}", headers=cabecalho_admin
        ).json()
        assert contrato["status"] == "EXPIRING"

    def test_idempotencia_garantida_pelo_indice_unico(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer, engine
    ) -> None:
        """Rodar duas vezes no mesmo dia não gera notificação duplicada."""
        from sqlalchemy import text

        api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})
        segunda = api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={}).json()

        assert segunda["alertas_gerados"] == 0

        with engine.connect() as conexao:
            total = conexao.execute(text("SELECT count(*) FROM notifications")).scalar_one()
            chaves = conexao.execute(
                text("SELECT count(DISTINCT dedupe_key) FROM notifications")
            ).scalar_one()
        assert total == chaves == 1

    def test_execucao_fica_registrada(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer
    ) -> None:
        api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})

        execucoes = api.get("/api/v1/jobs/runs", headers=cabecalho_admin).json()
        assert execucoes
        ultima = execucoes[0]
        assert ultima["status"] == "SUCCESS"
        assert ultima["duration_seconds"] is not None
        assert ultima["stats"]["contratos_analisados"] >= 1

    def test_notificacao_e_registrada_com_destinatario_mascarado(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer
    ) -> None:
        """O painel precisa dizer se o aviso saiu, não entregar a lista de
        e-mails dos clientes."""
        api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})

        notificacoes = api.get("/api/v1/notifications", headers=cabecalho_admin).json()
        assert notificacoes["total"] == 1

        item = notificacoes["items"][0]
        assert item["status"] == "SENT"
        assert item["recipient"].startswith("f***@")
        assert "financeiro@" not in item["recipient"]

    def test_chave_de_idempotencia_chega_ao_provedor(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer, notifier
    ) -> None:
        """O retry após timeout não pode duplicar a mensagem na caixa do
        cliente — quem descarta o repetido é o provedor, pela chave."""
        api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})
        enviada = notifier.enviadas[0]
        assert enviada.idempotency_key.startswith("CONTRACT_EXPIRING:")
        assert enviada.channel == "email"

    def test_reprocessar_data_passada(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer
    ) -> None:
        """Reprocessar um dia é uma operação segura, por construção."""
        ontem = (date.today() - timedelta(days=1)).isoformat()
        resposta = api.post(
            "/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={"reference": ontem}
        )
        assert resposta.status_code == 200
        assert resposta.json()["status"] == "SUCCESS"


class TestFalhaDeEnvio:
    def test_notificacao_que_falha_fica_pendente_e_e_reenviada(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer, notifier
    ) -> None:
        from app.domain.ports.notifier import NotificationResult

        notifier.respostas = [
            NotificationResult(delivered=False, error="503 do provedor", retryable=True)
        ]
        primeira = api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={}).json()
        assert primeira["envio"]["falhas"] == 1

        pendentes = api.get(
            "/api/v1/notifications", headers=cabecalho_admin, params={"status": "FAILED"}
        ).json()
        assert pendentes["total"] == 1

        # Sem roteiro, o dublê responde sucesso: a próxima execução reenvia.
        segunda = api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={}).json()
        assert segunda["envio"]["enviadas"] == 1

    def test_reenfileirar_manualmente_zera_as_tentativas(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer, notifier
    ) -> None:
        from app.domain.ports.notifier import NotificationResult

        falha = NotificationResult(delivered=False, error="503", retryable=True)
        notifier.respostas = [falha, falha, falha]
        for _ in range(3):
            api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})

        mortas = api.get(
            "/api/v1/notifications", headers=cabecalho_admin, params={"status": "DEAD_LETTER"}
        ).json()
        assert mortas["total"] == 1

        recolocada = api.post(
            f"/api/v1/notifications/{mortas['items'][0]['id']}/retry",
            headers=cabecalho_admin,
        )
        assert recolocada.status_code == 200
        assert recolocada.json()["status"] == "PENDING"
        assert recolocada.json()["attempts"] == 0

    def test_notificacao_ja_enviada_nao_e_reenfileirada(
        self, api: TestClient, cabecalho_admin, contrato_a_vencer
    ) -> None:
        api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={})
        enviada = api.get("/api/v1/notifications", headers=cabecalho_admin).json()["items"][0]

        resposta = api.post(f"/api/v1/notifications/{enviada['id']}/retry", headers=cabecalho_admin)
        assert resposta.status_code == 422


class TestContratoVencido:
    def test_marca_vencido_e_avisa(
        self, api: TestClient, cabecalho_admin, cabecalho_operador, cliente_criado
    ) -> None:
        hoje = date.today()
        contrato = api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json={
                "client_id": cliente_criado["id"],
                "number": "CT-JA-VENCEU",
                "monthly_amount": "500.00",
                "start_date": (hoje - timedelta(days=400)).isoformat(),
                "end_date": (hoje - timedelta(days=3)).isoformat(),
                "due_day": 10,
            },
        ).json()

        resultado = api.post(
            "/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={}
        ).json()
        assert resultado["contratos_expirados"] == 1

        atualizado = api.get(f"/api/v1/contracts/{contrato['id']}", headers=cabecalho_admin).json()
        assert atualizado["status"] == "EXPIRED"

    def test_renovacao_automatica_cria_sucessor(
        self, api: TestClient, cabecalho_admin, cabecalho_operador, cliente_criado
    ) -> None:
        hoje = date.today()
        api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json={
                "client_id": cliente_criado["id"],
                "number": "CT-AUTO-RENOVA",
                "monthly_amount": "750.00",
                "start_date": (hoje - timedelta(days=366)).isoformat(),
                "end_date": (hoje - timedelta(days=1)).isoformat(),
                "due_day": 15,
                "auto_renew": True,
                "renewal_term_months": 12,
            },
        )

        resultado = api.post(
            "/api/v1/jobs/daily-check/run", headers=cabecalho_admin, json={}
        ).json()
        assert resultado["renovacoes_automaticas"] == 1

        contratos = api.get(
            "/api/v1/contracts", headers=cabecalho_admin, params={"status": "ACTIVE"}
        ).json()
        assert any(c["number"] == "CT-AUTO-RENOVA-R1" for c in contratos["items"])
