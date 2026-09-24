"""Ciclo de vida do contrato pela API, contra o banco real.

O que só aqui é verificável: as restrições do PostgreSQL. Índice único de
número de contrato, `UNIQUE (contract_id, competence)` das parcelas, trava
otimista com `WHERE version = ...` e a imutabilidade do histórico.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _corpo_contrato(client_id: str, **ajustes) -> dict:  # type: ignore[no-untyped-def]
    hoje = date.today()
    corpo = {
        "client_id": client_id,
        "number": "CT-2026-9999",
        "monthly_amount": "1500.00",
        "start_date": (hoje - timedelta(days=30)).isoformat(),
        "end_date": (hoje + timedelta(days=335)).isoformat(),
        "due_day": 10,
    }
    corpo.update(ajustes)
    return corpo


class TestClientes:
    def test_documento_e_normalizado_e_unico(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        """O mesmo CNPJ com pontuação diferente não pode entrar duas vezes.

        A normalização (guardar só os dígitos) é o que faz a restrição de
        unicidade do banco de fato funcionar.
        """
        duplicado = api.post(
            "/api/v1/clients",
            headers=cabecalho_operador,
            json={
                "tax_id": "12345678000195",  # mesmo documento, sem pontuação
                "legal_name": "Outra Razão Social",
                "email": "outro@exemplo.com",
            },
        )
        assert duplicado.status_code == 409
        assert duplicado.json()["code"] == "conflict"

    def test_documento_invalido_e_recusado_com_422(
        self, api: TestClient, cabecalho_operador
    ) -> None:
        resposta = api.post(
            "/api/v1/clients",
            headers=cabecalho_operador,
            json={
                "tax_id": "12.345.678/0001-99",  # dígito verificador errado
                "legal_name": "Empresa Inexistente",
                "email": "x@exemplo.com",
            },
        )
        assert resposta.status_code == 422

    def test_campo_desconhecido_e_recusado(self, api: TestClient, cabecalho_operador) -> None:
        """`extra="forbid"`: tentativa de atribuição em massa vira erro visível.

        Sem isso, o campo extra seria ignorado em silêncio e quem chamou a API
        acharia que ele foi aceito.
        """
        resposta = api.post(
            "/api/v1/clients",
            headers=cabecalho_operador,
            json={
                "tax_id": "529.982.247-25",
                "legal_name": "Tentativa",
                "email": "x@exemplo.com",
                "status": "INACTIVE",  # não faz parte do schema de entrada
            },
        )
        assert resposta.status_code == 422

    def test_nao_desativa_com_contrato_vigente(
        self, api: TestClient, cabecalho_operador, cliente_criado, contrato_criado
    ) -> None:
        resposta = api.post(
            f"/api/v1/clients/{cliente_criado['id']}/deactivate",
            headers=cabecalho_operador,
            json={"reason": "encerramento de relacionamento"},
        )
        assert resposta.status_code == 422
        assert resposta.json()["details"]["contratos_vigentes"] == 1

    def test_busca_por_termo(self, api: TestClient, cabecalho_operador, cliente_criado) -> None:
        resposta = api.get("/api/v1/clients", headers=cabecalho_operador, params={"term": "Aurora"})
        assert resposta.json()["total"] == 1

    def test_busca_com_curinga_nao_casa_tudo(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        """`%` no termo de busca é dado, não sintaxe.

        Sem escapar, buscar por `%` devolveria a base inteira — e a paginação
        não impediria o vazamento, só o fatiaria.
        """
        resposta = api.get("/api/v1/clients", headers=cabecalho_operador, params={"term": "%"})
        assert resposta.json()["total"] == 0

    def test_termo_com_injecao_e_apenas_texto(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        """A prova prática de que a consulta é parametrizada.

        Se houvesse concatenação de string, este termo derrubaria a tabela.
        Como o valor viaja separado da consulta, ele é só um nome que não
        existe — e a tabela continua lá, o que o teste seguinte confirma.
        """
        resposta = api.get(
            "/api/v1/clients",
            headers=cabecalho_operador,
            params={"term": "'; DROP TABLE clients; --"},
        )
        assert resposta.status_code == 200
        assert resposta.json()["total"] == 0
        assert api.get("/api/v1/clients", headers=cabecalho_operador).json()["total"] == 1


class TestCriacaoDeContrato:
    def test_cria_e_gera_parcelas(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        parcelas = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}/installments",
            headers=cabecalho_operador,
        ).json()

        assert len(parcelas) >= 11
        competencias = [p["competence"] for p in parcelas]
        assert len(competencias) == len(set(competencias))
        assert all(p["status"] == "PENDING" for p in parcelas)

    def test_valor_monetario_atravessa_como_decimal(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        """`1234.56` precisa voltar como `1234.56`, não `1234.5599999999999`."""
        resposta = api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json=_corpo_contrato(
                cliente_criado["id"], number="CT-DECIMAL", monthly_amount="1234.56"
            ),
        )
        assert resposta.json()["monthly_amount"] == "1234.56"

    def test_numero_duplicado_e_recusado_pelo_banco(
        self, api: TestClient, cabecalho_operador, cliente_criado, contrato_criado
    ) -> None:
        duplicado = api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json=_corpo_contrato(cliente_criado["id"], number=contrato_criado["number"]),
        )
        assert duplicado.status_code == 409

    def test_periodo_invertido_e_recusado(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        hoje = date.today()
        resposta = api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json=_corpo_contrato(
                cliente_criado["id"],
                number="CT-INVERTIDO",
                start_date=hoje.isoformat(),
                end_date=(hoje - timedelta(days=10)).isoformat(),
            ),
        )
        assert resposta.status_code == 422

    def test_valor_negativo_e_recusado(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        resposta = api.post(
            "/api/v1/contracts",
            headers=cabecalho_operador,
            json=_corpo_contrato(
                cliente_criado["id"], number="CT-NEGATIVO", monthly_amount="-100.00"
            ),
        )
        assert resposta.status_code == 422


class TestTravaOtimista:
    def test_versao_divergente_responde_409(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        """Duas edições simultâneas: a segunda não sobrescreve a primeira.

        Sem a trava, o operador que reajustou o valor descobre semanas depois
        que a alteração dele desapareceu — sem erro, sem log, sem explicação.
        """
        primeira = api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"monthly_amount": "1600.00", "expected_version": contrato_criado["version"]},
        )
        assert primeira.status_code == 200
        assert primeira.json()["version"] == contrato_criado["version"] + 1

        segunda = api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"monthly_amount": "1700.00", "expected_version": contrato_criado["version"]},
        )
        assert segunda.status_code == 409
        assert segunda.json()["details"]["versao_atual"] == contrato_criado["version"] + 1

    def test_sem_versao_a_alteracao_passa(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        """A trava é opcional: cliente que não controla concorrência não é
        obrigado a enviar a versão."""
        resposta = api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"description": "sem controle de versão"},
        )
        assert resposta.status_code == 200


class TestRenovacao:
    def test_cria_sucessor_e_encerra_o_original(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        resposta = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={"term_months": 12, "adjustment_percent": "8.5"},
        )
        assert resposta.status_code == 201

        sucessor = resposta.json()
        assert sucessor["previous_contract_id"] == contrato_criado["id"]
        assert sucessor["number"] == f"{contrato_criado['number']}-R1"
        assert sucessor["monthly_amount"] == "1627.50"  # 1500 * 1.085

        original = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}", headers=cabecalho_operador
        ).json()
        assert original["status"] == "RENEWED"
        assert original["renewed_to_id"] == sucessor["id"]

    def test_periodos_nao_se_sobrepoem(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        """Um dia coberto por dois contratos é um dia cobrado duas vezes."""
        sucessor = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={},
        ).json()

        fim_original = date.fromisoformat(contrato_criado["end_date"])
        inicio_sucessor = date.fromisoformat(sucessor["start_date"])
        assert (inicio_sucessor - fim_original).days == 1

    def test_sucessor_tem_as_proprias_parcelas(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        sucessor = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={"term_months": 6},
        ).json()

        parcelas = api.get(
            f"/api/v1/contracts/{sucessor['id']}/installments", headers=cabecalho_operador
        ).json()
        assert len(parcelas) >= 5
        assert all(p["contract_id"] == sucessor["id"] for p in parcelas)

    def test_contrato_renovado_nao_renova_de_novo(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={},
        )
        segunda = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={},
        )
        assert segunda.status_code == 422


class TestCancelamento:
    def test_cancela_e_bloqueia_operacoes_seguintes(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        cancelado = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/cancel",
            headers=cabecalho_operador,
            json={"reason": "rescisão solicitada pelo cliente"},
        )
        assert cancelado.status_code == 200
        assert cancelado.json()["status"] == "CANCELLED"
        assert cancelado.json()["cancelled_at"] is not None

        for caminho, corpo in (
            (f"/api/v1/contracts/{contrato_criado['id']}/renew", {}),
            (f"/api/v1/contracts/{contrato_criado['id']}/cancel", {"reason": "de novo"}),
        ):
            assert api.post(caminho, headers=cabecalho_operador, json=corpo).status_code == 422

    def test_motivo_e_obrigatorio(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        resposta = api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/cancel",
            headers=cabecalho_operador,
            json={},
        )
        assert resposta.status_code == 422


class TestHistorico:
    def test_registra_a_linha_do_tempo_completa(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"monthly_amount": "1800.00"},
        )
        api.post(
            f"/api/v1/contracts/{contrato_criado['id']}/renew",
            headers=cabecalho_operador,
            json={},
        )

        historico = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}/history", headers=cabecalho_operador
        ).json()
        tipos = {evento["event_type"] for evento in historico}
        assert {"CREATED", "UPDATED", "RENEWED"} <= tipos

    def test_alteracao_guarda_o_antes_e_o_depois(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        """ "Contrato alterado" sozinho não responde à pergunta que alguém vai
        fazer daqui a seis meses."""
        api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"monthly_amount": "1800.00"},
        )
        historico = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}/history", headers=cabecalho_operador
        ).json()
        alteracao = next(e for e in historico if e["event_type"] == "UPDATED")
        assert alteracao["payload"]["alteracoes"]["monthly_amount"] == {
            "de": "1500.00",
            "para": "1800.00",
        }


class TestMensalidades:
    def test_quitacao_registra_data(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        parcelas = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}/installments",
            headers=cabecalho_operador,
        ).json()

        quitada = api.post(
            f"/api/v1/contracts/installments/{parcelas[0]['id']}/settle",
            headers=cabecalho_operador,
        )
        assert quitada.status_code == 200
        assert quitada.json()["status"] == "PAID"
        assert quitada.json()["paid_at"] is not None

    def test_quitar_duas_vezes_e_recusado(
        self, api: TestClient, cabecalho_operador, contrato_criado
    ) -> None:
        parcelas = api.get(
            f"/api/v1/contracts/{contrato_criado['id']}/installments",
            headers=cabecalho_operador,
        ).json()
        api.post(
            f"/api/v1/contracts/installments/{parcelas[0]['id']}/settle",
            headers=cabecalho_operador,
        )
        segunda = api.post(
            f"/api/v1/contracts/installments/{parcelas[0]['id']}/settle",
            headers=cabecalho_operador,
        )
        assert segunda.status_code == 422

    def test_prorrogar_vigencia_nao_duplica_competencia(
        self, api: TestClient, cabecalho_operador, contrato_criado, engine
    ) -> None:
        """A restrição `UNIQUE (contract_id, competence)` em ação.

        A geração de parcelas roda de novo ao prorrogar o contrato, e é o
        `ON CONFLICT DO NOTHING` que impede a segunda cobrança da mesma
        competência — não uma checagem em Python.
        """
        from sqlalchemy import text

        fim_novo = date.fromisoformat(contrato_criado["end_date"]) + timedelta(days=180)
        resposta = api.patch(
            f"/api/v1/contracts/{contrato_criado['id']}",
            headers=cabecalho_operador,
            json={"end_date": fim_novo.isoformat()},
        )
        assert resposta.status_code == 200

        with engine.connect() as conexao:
            duplicadas = conexao.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT contract_id, competence FROM installments"
                    "  GROUP BY contract_id, competence HAVING count(*) > 1"
                    ") d"
                )
            ).scalar_one()
        assert duplicadas == 0
