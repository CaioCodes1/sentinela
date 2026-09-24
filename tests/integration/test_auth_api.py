"""Autenticação pela API real, contra o banco real."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import Role

pytestmark = pytest.mark.integration

SENHA = "Sentinela#2026-Forte"


class TestLogin:
    def test_login_e_acesso_a_rota_protegida(self, api: TestClient, operador) -> None:
        resposta = api.post(
            "/api/v1/auth/login",
            json={"email": str(operador.email), "password": SENHA},
        )
        assert resposta.status_code == 200
        corpo = resposta.json()
        assert corpo["token_type"] == "Bearer"
        assert corpo["role"] == "OPERATOR"

        me = api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {corpo['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["email"] == str(operador.email)
        assert "contract:create" in me.json()["permissions"]

    def test_sem_token_responde_401_no_formato_rfc7807(self, api: TestClient) -> None:
        resposta = api.get("/api/v1/clients")
        assert resposta.status_code == 401
        assert resposta.headers["content-type"].startswith("application/problem+json")

        corpo = resposta.json()
        assert corpo["code"] == "authentication_failed"
        assert corpo["status"] == 401
        # O `request_id` é o que liga esta resposta opaca ao log do servidor.
        assert corpo["request_id"]
        assert resposta.headers["WWW-Authenticate"] == "Bearer"

    @pytest.mark.parametrize(
        "cabecalho",
        [
            {"Authorization": "Bearer nao-e-um-token"},
            {"Authorization": "Basic YWRtaW46YWRtaW4="},
            {"Authorization": "Bearer"},
            {"Authorization": ""},
        ],
    )
    def test_credencial_malformada_e_recusada(self, api: TestClient, cabecalho) -> None:
        assert api.get("/api/v1/clients", headers=cabecalho).status_code == 401

    def test_usuario_desativado_nao_entra(self, api: TestClient, criar_usuario) -> None:
        inativo = criar_usuario(email="inativo@sentinela.local", papel=Role.OPERATOR, ativo=False)
        resposta = api.post(
            "/api/v1/auth/login", json={"email": str(inativo.email), "password": SENHA}
        )
        assert resposta.status_code == 401
        assert resposta.json()["detail"] == "credenciais inválidas"

    def test_token_de_usuario_desativado_depois_do_login_para_de_valer(
        self, api: TestClient, operador, cabecalho_operador, engine
    ) -> None:
        """O motivo de consultar o banco a cada requisição.

        O access token continua com assinatura válida pelos seus 15 minutos. Se
        a autorização confiasse só no token, desativar um usuário — uma
        demissão, por exemplo — não teria efeito imediato.
        """
        from sqlalchemy import text

        assert api.get("/api/v1/clients", headers=cabecalho_operador).status_code == 200

        with engine.begin() as conexao:
            conexao.execute(
                text("UPDATE users SET is_active = false WHERE id = :id"),
                {"id": operador.id},
            )

        assert api.get("/api/v1/clients", headers=cabecalho_operador).status_code == 401


class TestBloqueioDeConta:
    def test_bloqueia_apos_tentativas_e_responde_423(
        self, api: TestClient, api_settings, operador
    ) -> None:
        for _ in range(api_settings.login_max_attempts):
            resposta = api.post(
                "/api/v1/auth/login",
                json={"email": str(operador.email), "password": "ErradaDeProposito#1"},
            )
            assert resposta.status_code == 401

        bloqueada = api.post(
            "/api/v1/auth/login",
            json={"email": str(operador.email), "password": SENHA},
        )
        assert bloqueada.status_code == 423
        assert bloqueada.json()["code"] == "account_locked"

    def test_contador_persiste_no_banco(self, api: TestClient, operador, engine) -> None:
        """A prova de que o commit explícito na falha de login funciona.

        O fluxo termina levantando exceção, que provoca rollback. Sem o commit
        antes do `raise`, o contador voltaria a zero a cada tentativa e o
        bloqueio jamais aconteceria — o defeito passaria em todo teste
        unitário que usasse repositório em memória.
        """
        from sqlalchemy import text

        api.post(
            "/api/v1/auth/login",
            json={"email": str(operador.email), "password": "Errada#1"},
        )
        with engine.connect() as conexao:
            tentativas = conexao.execute(
                text("SELECT failed_login_attempts FROM users WHERE id = :id"),
                {"id": operador.id},
            ).scalar_one()
        assert tentativas == 1


class TestRotacaoDeToken:
    def _login(self, api: TestClient, email: str) -> dict:
        return api.post("/api/v1/auth/login", json={"email": email, "password": SENHA}).json()

    def test_refresh_rotaciona_e_invalida_o_antigo(self, api: TestClient, operador) -> None:
        primeiro = self._login(api, str(operador.email))

        segundo = api.post(
            "/api/v1/auth/refresh", json={"refresh_token": primeiro["refresh_token"]}
        )
        assert segundo.status_code == 200
        assert segundo.json()["refresh_token"] != primeiro["refresh_token"]

        reuso = api.post("/api/v1/auth/refresh", json={"refresh_token": primeiro["refresh_token"]})
        assert reuso.status_code == 401

    def test_reuso_derruba_a_familia_e_fica_na_auditoria(
        self, api: TestClient, operador, cabecalho_auditor
    ) -> None:
        primeiro = self._login(api, str(operador.email))
        segundo = api.post(
            "/api/v1/auth/refresh", json={"refresh_token": primeiro["refresh_token"]}
        ).json()

        api.post("/api/v1/auth/refresh", json={"refresh_token": primeiro["refresh_token"]})

        # O token legítimo, ainda não usado, também é invalidado.
        assert (
            api.post(
                "/api/v1/auth/refresh", json={"refresh_token": segundo["refresh_token"]}
            ).status_code
            == 401
        )

        trilha = api.get(
            "/api/v1/audit/logs",
            headers=cabecalho_auditor,
            params={"action": "TOKEN_REUSE_DETECTED"},
        ).json()
        assert trilha["total"] >= 1

    def test_refresh_token_e_guardado_como_hash(self, api: TestClient, operador, engine) -> None:
        """Um dump do banco não pode entregar sessões utilizáveis."""
        from sqlalchemy import text

        par = self._login(api, str(operador.email))
        with engine.connect() as conexao:
            hashes = [
                linha[0] for linha in conexao.execute(text("SELECT token_hash FROM refresh_tokens"))
            ]
        assert hashes
        assert par["refresh_token"] not in hashes
        assert all(len(h) == 64 for h in hashes)

    def test_logout_global_derruba_todas_as_sessoes(self, api: TestClient, operador) -> None:
        primeira = self._login(api, str(operador.email))
        segunda = self._login(api, str(operador.email))

        api.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {primeira['access_token']}"},
            json={"all_sessions": True},
        )
        for par in (primeira, segunda):
            assert (
                api.post(
                    "/api/v1/auth/refresh", json={"refresh_token": par["refresh_token"]}
                ).status_code
                == 401
            )


class TestCriacaoDeUsuario:
    def test_admin_cria_usuario(self, api: TestClient, cabecalho_admin) -> None:
        resposta = api.post(
            "/api/v1/auth/users",
            headers=cabecalho_admin,
            json={
                "email": "novo@sentinela.local",
                "password": "OutraChave#2026Forte",
                "full_name": "Usuário Novo",
                "role": "AUDITOR",
            },
        )
        assert resposta.status_code == 201
        assert resposta.json()["role"] == "AUDITOR"

    def test_email_duplicado_ignorando_caixa(
        self, api: TestClient, cabecalho_admin, operador
    ) -> None:
        """O índice único sobre `lower(email)`, que só existe na migration.

        Com `UNIQUE(email)` comum, `OPERADOR@...` e `operador@...` seriam duas
        contas — e o login pela forma normalizada encontraria duas linhas.
        """
        resposta = api.post(
            "/api/v1/auth/users",
            headers=cabecalho_admin,
            json={
                "email": "OPERADOR@Sentinela.LOCAL",
                "password": "OutraChave#2026Forte",
                "full_name": "Clone",
                "role": "OPERATOR",
            },
        )
        assert resposta.status_code == 409

    def test_senha_nunca_aparece_na_resposta(self, api: TestClient, cabecalho_admin) -> None:
        resposta = api.post(
            "/api/v1/auth/users",
            headers=cabecalho_admin,
            json={
                "email": "sem-vazamento@sentinela.local",
                "password": "OutraChave#2026Forte",
                "full_name": "Sem Vazamento",
                "role": "OPERATOR",
            },
        )
        corpo = resposta.text
        assert "OutraChave" not in corpo
        assert "password" not in resposta.json()

    def test_hash_no_banco_nao_e_a_senha(self, api: TestClient, cabecalho_admin, engine) -> None:
        from sqlalchemy import text

        api.post(
            "/api/v1/auth/users",
            headers=cabecalho_admin,
            json={
                "email": "hash@sentinela.local",
                "password": "OutraChave#2026Forte",
                "full_name": "Com Hash",
                "role": "OPERATOR",
            },
        )
        with engine.connect() as conexao:
            hash_gravado = conexao.execute(
                text("SELECT password_hash FROM users WHERE email = 'hash@sentinela.local'")
            ).scalar_one()
        assert hash_gravado.startswith("$2b$")
        assert "OutraChave" not in hash_gravado
