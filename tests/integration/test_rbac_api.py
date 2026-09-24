"""Autorização na API real, perfil por perfil.

O teste mais importante deste arquivo é `test_toda_rota_exige_autorizacao`: ele
varre o aplicativo montado e reprova qualquer rota que tenha entrado sem
dependência de autenticação. É a defesa contra o esquecimento — o endpoint novo
que alguém acrescenta numa sexta-feira sem o `Depends(require(...))`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

# Rotas deliberadamente públicas. Qualquer outra sem proteção reprova.
PUBLICAS = {
    "/health",
    "/health/live",
    "/health/ready",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


class TestVarreduraDeRotas:
    def test_toda_rota_exige_autorizacao(self, api: TestClient) -> None:
        """Nenhuma rota sem autenticação além da lista acima.

        A checagem é sobre o comportamento, não sobre a declaração: bate-se em
        cada caminho sem credencial e exige-se 401. Verificar a presença do
        `Depends` seria mais frágil — passaria numa rota que declara a
        dependência e a ignora.
        """
        desprotegidas: list[str] = []
        spec = api.app.openapi()  # type: ignore[attr-defined]

        for caminho, operacoes in spec["paths"].items():
            if caminho in PUBLICAS:
                continue
            # Substitui parâmetros de caminho por um UUID válido, para que o
            # 401 venha da autorização e não da validação do parâmetro.
            alvo = caminho
            for parametro in (
                "{client_id}",
                "{contract_id}",
                "{installment_id}",
                "{notification_id}",
                "{resource_type}",
                "{resource_id}",
            ):
                alvo = alvo.replace(parametro, "00000000-0000-4000-8000-000000000000")

            for metodo in operacoes:
                resposta = api.request(metodo.upper(), alvo, json={})
                if resposta.status_code != 401:
                    desprotegidas.append(f"{metodo.upper()} {caminho} -> {resposta.status_code}")

        assert not desprotegidas, f"rotas sem exigir autenticação: {desprotegidas}"


class TestOperador:
    def test_cria_e_edita_cliente(self, api: TestClient, cabecalho_operador) -> None:
        criado = api.post(
            "/api/v1/clients",
            headers=cabecalho_operador,
            json={
                "tax_id": "529.982.247-25",
                "legal_name": "João da Silva ME",
                "email": "joao@exemplo.com",
            },
        )
        assert criado.status_code == 201

        alterado = api.patch(
            f"/api/v1/clients/{criado.json()['id']}",
            headers=cabecalho_operador,
            json={"legal_name": "João da Silva EIRELI"},
        )
        assert alterado.status_code == 200

    def test_nao_le_auditoria(self, api: TestClient, cabecalho_operador) -> None:
        """Quem é auditado não decide o que a auditoria mostra."""
        resposta = api.get("/api/v1/audit/logs", headers=cabecalho_operador)
        assert resposta.status_code == 403
        assert resposta.json()["code"] == "not_authorized"

    def test_nao_cria_usuario(self, api: TestClient, cabecalho_operador) -> None:
        """Criar usuário é escalada de privilégio por definição."""
        resposta = api.post(
            "/api/v1/auth/users",
            headers=cabecalho_operador,
            json={
                "email": "intruso@sentinela.local",
                "password": "OutraChave#2026Forte",
                "full_name": "Intruso",
                "role": "ADMIN",
            },
        )
        assert resposta.status_code == 403

    def test_nao_dispara_a_automacao(self, api: TestClient, cabecalho_operador) -> None:
        """Disparar o job manda e-mail de verdade para cliente de verdade."""
        resposta = api.post("/api/v1/jobs/daily-check/run", headers=cabecalho_operador)
        assert resposta.status_code == 403


class TestAuditor:
    def test_le_contrato(self, api: TestClient, cabecalho_auditor, contrato_criado) -> None:
        resposta = api.get("/api/v1/contracts", headers=cabecalho_auditor)
        assert resposta.status_code == 200
        assert resposta.json()["total"] == 1

    def test_le_auditoria(self, api: TestClient, cabecalho_auditor, contrato_criado) -> None:
        resposta = api.get("/api/v1/audit/logs", headers=cabecalho_auditor)
        assert resposta.status_code == 200
        assert resposta.json()["total"] >= 1

    @pytest.mark.parametrize(
        ("metodo", "caminho", "corpo"),
        [
            (
                "POST",
                "/api/v1/clients",
                {"tax_id": "529.982.247-25", "legal_name": "Tentativa", "email": "x@y.com"},
            ),
            ("POST", "/api/v1/contracts", {}),
            ("POST", "/api/v1/jobs/daily-check/run", {}),
        ],
    )
    def test_nenhuma_escrita_passa(
        self, api: TestClient, cabecalho_auditor, metodo, caminho, corpo
    ) -> None:
        """A propriedade central do perfil somente-leitura.

        Note que o 403 vem **antes** da validação do corpo: enviar `{}` para
        `/contracts` daria 422 se a autorização deixasse passar. O 403
        confirma que a barreira está na ordem certa.
        """
        resposta = api.request(metodo, caminho, headers=cabecalho_auditor, json=corpo)
        assert resposta.status_code == 403


class TestAdmin:
    def test_ve_a_matriz_de_permissoes(self, api: TestClient, cabecalho_admin) -> None:
        resposta = api.get("/api/v1/admin/permissions", headers=cabecalho_admin)
        assert resposta.status_code == 200

        matriz = resposta.json()
        assert set(matriz) == {"ADMIN", "OPERATOR", "AUDITOR"}
        assert "audit:read" in matriz["AUDITOR"]
        assert "audit:read" not in matriz["OPERATOR"]

    def test_faz_tudo_o_que_o_operador_faz(self, api: TestClient, cabecalho_admin) -> None:
        resposta = api.post(
            "/api/v1/clients",
            headers=cabecalho_admin,
            json={
                "tax_id": "529.982.247-25",
                "legal_name": "Cliente do Admin",
                "email": "admin-cliente@exemplo.com",
            },
        )
        assert resposta.status_code == 201


class TestAuditoriaDeNegativa:
    def test_acesso_negado_vai_para_a_trilha(
        self, api: TestClient, cabecalho_operador, cabecalho_auditor
    ) -> None:
        """Tentativa de acesso negado é o evento que uma investigação procura.

        Sem este registro, o 403 existe apenas como resposta HTTP e desaparece
        assim que o log de acesso rotaciona.
        """
        api.get("/api/v1/audit/logs", headers=cabecalho_operador)

        trilha = api.get(
            "/api/v1/audit/logs",
            headers=cabecalho_auditor,
            params={"action": "ACCESS_DENIED"},
        ).json()

        assert trilha["total"] >= 1
        registro = trilha["items"][0]
        assert registro["outcome"] == "FAILURE"
        assert registro["actor_role"] == "OPERATOR"
        assert "audit:read" in registro["resource_id"]
