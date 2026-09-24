"""As garantias que moram no banco, e não no código Python.

Tudo aqui continua valendo para quem abrir um `psql` e escrever SQL à mão — que
é a diferença entre uma regra de aplicação e uma regra de dados.

Inclui também o formato das respostas de erro e os cabeçalhos de segurança, que
só existem de verdade quando a aplicação está montada.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

pytestmark = pytest.mark.integration


class TestImutabilidadeDaAuditoria:
    def test_update_direto_no_banco_e_recusado(
        self, api: TestClient, cabecalho_operador, cliente_criado, engine
    ) -> None:
        """Nem com SQL direto.

        A proteção é um gatilho `BEFORE UPDATE OR DELETE`, e não `REVOKE`: o
        `REVOKE` é ignorado por superusuário e pode ser desfeito pelo dono da
        tabela com uma linha de `GRANT`. O gatilho vale para todo mundo.
        """
        with engine.connect() as conexao:
            total = conexao.execute(text("SELECT count(*) FROM audit_logs")).scalar_one()
        assert total >= 1, "o cadastro de cliente deveria ter gerado auditoria"

        with pytest.raises(DatabaseError) as erro:
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE audit_logs SET action = 'LOGOUT'"))
        assert "somente-inserção" in str(erro.value)

    def test_delete_direto_no_banco_e_recusado(
        self, api: TestClient, cabecalho_operador, cliente_criado, engine
    ) -> None:
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("DELETE FROM audit_logs"))

        with engine.connect() as conexao:
            restantes = conexao.execute(text("SELECT count(*) FROM audit_logs")).scalar_one()
        assert restantes >= 1

    def test_historico_do_contrato_tem_a_mesma_protecao(
        self, api: TestClient, contrato_criado, engine
    ) -> None:
        """Histórico alterável é histórico que não serve de prova."""
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE contract_events SET event_type = 'FALSO'"))

    def test_truncate_continua_funcionando(self, api: TestClient, contrato_criado, engine) -> None:
        """A porta de saída legítima, pensada junto com a proteção.

        `TRUNCATE` não dispara gatilho de linha. É o que viabiliza política de
        retenção e limpeza entre testes sem abrir brecha para adulterar linha a
        linha — que é o ataque que a imutabilidade existe para impedir.
        """
        with engine.begin() as conexao:
            conexao.execute(text("TRUNCATE audit_logs"))
            restantes = conexao.execute(text("SELECT count(*) FROM audit_logs")).scalar_one()
        assert restantes == 0


class TestRestricoesDeIntegridade:
    def test_check_impede_mensalidade_negativa_por_sql(
        self, api: TestClient, contrato_criado, engine
    ) -> None:
        """A validação do Pydantic protege a API; o `CHECK` protege o dado.

        Um script de importação rodado às pressas não passa pelo Pydantic.
        """
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE contracts SET monthly_amount = -1"))

    def test_check_impede_periodo_invertido_por_sql(
        self, api: TestClient, contrato_criado, engine
    ) -> None:
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE contracts SET end_date = start_date - 1"))

    def test_cancelado_sem_data_e_recusado(self, api: TestClient, contrato_criado, engine) -> None:
        """Coerência entre status e colunas de apoio.

        Sem este `CHECK`, existiria contrato `CANCELLED` sem `cancelled_at` — e
        o relatório de cancelamentos do mês deixaria de contá-lo, em silêncio.
        """
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("UPDATE contracts SET status = 'CANCELLED'"))

    def test_cliente_com_contrato_nao_pode_ser_apagado(
        self, api: TestClient, contrato_criado, engine
    ) -> None:
        """`ON DELETE RESTRICT`: apagar o cliente não leva junto o histórico."""
        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(text("DELETE FROM clients"))

    def test_usuario_com_trilha_nao_pode_ser_removido(
        self, api: TestClient, cabecalho_operador, operador, cliente_criado, engine
    ) -> None:
        """`ON DELETE RESTRICT` na auditoria, e o porquê de não ser `SET NULL`.

        `SET NULL` foi a primeira escolha, com a intenção certa — preservar a
        trilha de quem foi removido. Ela é **inexequível** aqui: o PostgreSQL
        implementa `SET NULL` como um UPDATE na tabela referenciada, e o
        gatilho de imutabilidade recusa UPDATE. A remoção de usuário falhava
        com erro do gatilho, e a cláusula era código morto.

        Com `RESTRICT`, a regra corresponde à política real do domínio: usuário
        é desativado, não apagado. Ver a migration `c9d8e7f6a5b4`.
        """
        with engine.connect() as conexao:
            trilha = conexao.execute(
                text("SELECT count(*) FROM audit_logs WHERE actor_user_id = :id"),
                {"id": operador.id},
            ).scalar_one()
        assert trilha >= 1

        with pytest.raises(DatabaseError):
            with engine.begin() as conexao:
                conexao.execute(
                    text("DELETE FROM refresh_tokens WHERE user_id = :id"), {"id": operador.id}
                )
                conexao.execute(text("UPDATE clients SET created_by_id = NULL"))
                conexao.execute(text("UPDATE contracts SET created_by_id = NULL"))
                conexao.execute(text("DELETE FROM users WHERE id = :id"), {"id": operador.id})

    def test_trilha_guarda_email_e_papel_do_momento_do_ato(
        self, api: TestClient, cabecalho_operador, operador, cliente_criado, engine
    ) -> None:
        """A cópia que sobrevive a mudanças no cadastro.

        Se o usuário for renomeado ou promovido depois, a trilha continua
        dizendo quem ele era naquele dia — que é o ponto de auditoria.
        """
        with engine.connect() as conexao:
            linha = conexao.execute(
                text(
                    "SELECT actor_email, actor_role FROM audit_logs "
                    "WHERE actor_user_id = :id LIMIT 1"
                ),
                {"id": operador.id},
            ).one()
        assert linha.actor_email == str(operador.email)
        assert linha.actor_role == "OPERATOR"


class TestRevogacaoEmMassa:
    def test_logout_global_conta_as_sessoes_certas(self, api: TestClient, operador) -> None:
        """Confere que o `rowcount` de `UPDATE` é confiável neste driver.

        O `rowcount` **não** é confiável em `INSERT ... ON CONFLICT` com
        psycopg3 (devolve -1), e este teste existe para fixar que em `UPDATE`
        ele é — evitando que uma "correção" futura troque o mecanismo aqui sem
        necessidade, ou deixe de trocar onde é preciso.
        """
        senha = "Sentinela#2026-Forte"
        for _ in range(3):
            api.post("/api/v1/auth/login", json={"email": str(operador.email), "password": senha})

        ultimo = api.post(
            "/api/v1/auth/login", json={"email": str(operador.email), "password": senha}
        ).json()

        resposta = api.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {ultimo['access_token']}"},
            json={"all_sessions": True},
        )
        assert resposta.json()["message"].startswith("4 sessão")


class TestCabecalhosDeSeguranca:
    def test_api_recebe_csp_estrita(self, api: TestClient) -> None:
        resposta = api.get("/health")
        assert resposta.headers["Content-Security-Policy"] == (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )

    def test_documentacao_recebe_csp_propria(self, api: TestClient) -> None:
        """A lição que custou caro em outro projeto.

        Com `default-src 'none'` valendo também para `/docs`, o Swagger UI abre
        **em branco**: o CSS e o JS da própria documentação são bloqueados pela
        política. Nenhum teste de API pega isso, porque teste de API não
        renderiza página — só abrindo no navegador.
        """
        resposta = api.get("/docs")
        assert resposta.status_code == 200
        csp = resposta.headers["Content-Security-Policy"]
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp
        assert "default-src 'none'" not in csp

    @pytest.mark.parametrize(
        ("cabecalho", "esperado"),
        [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Cache-Control", "no-store"),
        ],
    )
    def test_cabecalhos_de_defesa(self, api: TestClient, cabecalho, esperado) -> None:
        assert api.get("/health").headers[cabecalho] == esperado

    def test_hsts_nao_aparece_fora_de_producao(self, api: TestClient) -> None:
        """Em desenvolvimento, o HSTS faria o navegador forçar HTTPS em
        `localhost` — e a política fica memorizada, quebrando o acesso local
        mesmo depois de removida."""
        assert "Strict-Transport-Security" not in api.get("/health").headers

    def test_request_id_volta_na_resposta(self, api: TestClient) -> None:
        resposta = api.get("/health", headers={"X-Request-Id": "minha-correlacao-123"})
        assert resposta.headers["X-Request-Id"] == "minha-correlacao-123"

    def test_request_id_do_cliente_e_saneado(self, api: TestClient) -> None:
        """Texto de fora que vai para o log e para a auditoria.

        Sem sanear, uma quebra de linha permitiria forjar linhas de log
        inteiras — injeção de log, que atrapalha exatamente a investigação que
        o campo deveria ajudar.
        """
        resposta = api.get("/health", headers={"X-Request-Id": "abc\r\nFAKE: linha forjada"})
        devolvido = resposta.headers["X-Request-Id"]
        assert "\n" not in devolvido and "\r" not in devolvido
        assert ":" not in devolvido


class TestFormatoDeErro:
    def test_erro_de_schema_nao_devolve_o_valor_rejeitado(self, api: TestClient) -> None:
        """O erro cru do Pydantic inclui `input` — ou seja, a senha digitada.

        Num `POST /auth/login` malformado, isso significa a senha dentro do
        corpo da resposta de erro, que o cliente costuma registrar em log.
        """
        resposta = api.post("/api/v1/auth/login", json={"email": "x@y.com", "password": ""})
        assert resposta.status_code == 422
        corpo = resposta.text
        assert "input" not in resposta.json().get("errors", [{}])[0]
        assert "campo" in resposta.json()["errors"][0]
        assert "senha-secreta" not in corpo

    def test_recurso_inexistente_responde_404_no_padrao(
        self, api: TestClient, cabecalho_operador
    ) -> None:
        resposta = api.get(
            "/api/v1/contracts/00000000-0000-4000-8000-000000000000",
            headers=cabecalho_operador,
        )
        assert resposta.status_code == 404
        assert resposta.headers["content-type"].startswith("application/problem+json")

        corpo = resposta.json()
        assert corpo["code"] == "not_found"
        assert corpo["request_id"]

    def test_erro_nao_vaza_detalhe_do_banco(
        self, api: TestClient, cabecalho_operador, cliente_criado
    ) -> None:
        """Mensagem do PostgreSQL traz nome de tabela, de restrição e o valor
        que colidiu — mapa gratuito do schema para quem está sondando."""
        duplicado = api.post(
            "/api/v1/clients",
            headers=cabecalho_operador,
            json={
                "tax_id": "12345678000195",
                "legal_name": "Duplicado",
                "email": "dup@exemplo.com",
            },
        )
        assert duplicado.status_code == 409
        corpo = duplicado.text.lower()
        for vazamento in ("psycopg", "duplicate key", "uq_clients", "constraint", "select"):
            assert vazamento not in corpo

    def test_corpo_acima_do_limite_e_recusado(self, api: TestClient, cabecalho_operador) -> None:
        gigante = {"legal_name": "x" * 400_000}
        resposta = api.patch(
            "/api/v1/clients/00000000-0000-4000-8000-000000000000",
            headers=cabecalho_operador,
            json=gigante,
        )
        assert resposta.status_code == 413
