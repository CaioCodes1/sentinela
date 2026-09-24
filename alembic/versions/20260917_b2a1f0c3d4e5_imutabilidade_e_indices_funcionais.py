"""imutabilidade da auditoria e índices funcionais

Esta migration é escrita à mão porque o autogenerate do Alembic não enxerga
gatilho, função nem índice sobre expressão. Tudo aqui é defesa que precisa
valer **no banco**, para todo mundo que se conectar a ele — não só para a
aplicação.

Revision ID: b2a1f0c3d4e5
Revises: c77c6a96d280
Create Date: 2026-09-17

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b2a1f0c3d4e5"
down_revision: str | None = "c77c6a96d280"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Por que GATILHO e não `REVOKE UPDATE, DELETE`
#
# `REVOKE` é a resposta que primeiro ocorre, e ela tem duas falhas conhecidas:
#
# 1. **Superusuário ignora permissão.** Em desenvolvimento e em teste a conexão
#    costuma ser superusuário, então um teste de imutabilidade baseado em
#    `REVOKE` passa verde sem testar absolutamente nada — o pior tipo de teste,
#    o que dá confiança falsa.
# 2. **O dono da tabela pode se reconceder o que perdeu.** `GRANT UPDATE ON
#    audit_logs TO ...` é uma linha para quem tiver acesso ao banco.
#
# O gatilho `BEFORE UPDATE OR DELETE` vale para superusuário, para o dono e
# para qualquer sessão. É a única barreira que não depende de quem está
# conectado.
#
# A porta de saída legítima é `TRUNCATE`: ele **não dispara gatilho de linha**,
# o que deixa a política de retenção e a limpeza entre testes viáveis sem abrir
# brecha para adulteração linha a linha — que é o ataque que importa aqui.
# ---------------------------------------------------------------------------
FUNCAO_IMUTAVEL = """
CREATE OR REPLACE FUNCTION recusar_alteracao_de_registro_imutavel()
RETURNS TRIGGER
LANGUAGE plpgsql
-- `SECURITY INVOKER` e `search_path` fixo: sem o search_path travado, um
-- schema malicioso no caminho de busca poderia sombrear uma função usada aqui
-- dentro. É a recomendação padrão para qualquer função em PL/pgSQL.
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $funcao$
BEGIN
    RAISE EXCEPTION
        'a tabela % é somente-inserção: % não é permitido (registro %)',
        TG_TABLE_NAME, TG_OP, COALESCE(OLD.id::text, '?')
        USING ERRCODE = 'restrict_violation',
              HINT = 'Trilhas de auditoria e histórico não podem ser alteradas nem removidas.';
END;
$funcao$;
"""


def upgrade() -> None:
    # -- 1. Unicidade de e-mail sem diferenciar maiúsculas -------------------
    # A restrição `UNIQUE` comum permitiria `Ana@x.com` e `ana@x.com` como duas
    # contas. O índice funcional fecha isso, **e** é o índice que o
    # `get_by_email` usa: o repositório compara `lower(email) = lower(:email)`
    # justamente para casar com ele. Sem o índice, aquela consulta faria
    # varredura sequencial na tabela de usuários a cada tentativa de login.
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")

    # -- 2. Imutabilidade da auditoria e do histórico ------------------------
    op.execute(FUNCAO_IMUTAVEL)

    for tabela in ("audit_logs", "contract_events"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{tabela}_somente_insercao
            BEFORE UPDATE OR DELETE ON {tabela}
            FOR EACH ROW
            EXECUTE FUNCTION recusar_alteracao_de_registro_imutavel();
            """
        )

    # -- 3. Índice para a busca de cliente por nome --------------------------
    # `ILIKE '%termo%'` não usa índice B-tree comum — o curinga à esquerda
    # impede. `pg_trgm` com índice GIN resolve. A extensão é criada com
    # `IF NOT EXISTS` porque em alguns ambientes gerenciados ela já vem, e em
    # outros o usuário da aplicação não tem permissão para criá-la; por isso o
    # bloco tolera a falha em vez de derrubar a migration inteira.
    op.execute(
        """
        DO $bloco$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE INDEX IF NOT EXISTS ix_clients_legal_name_trgm
                ON clients USING gin (legal_name gin_trgm_ops);
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'sem permissão para criar pg_trgm; busca por nome usará varredura';
        END
        $bloco$;
        """
    )

    # -- 4. Comentários no schema -------------------------------------------
    # Ficam no banco e aparecem em qualquer ferramenta de inspeção, inclusive
    # para quem abrir o `psql` daqui a dois anos sem acesso a este repositório.
    op.execute(
        "COMMENT ON TABLE audit_logs IS "
        "'Trilha de auditoria. Somente inserção: gatilho recusa UPDATE e DELETE.'"
    )
    op.execute(
        "COMMENT ON TABLE contract_events IS "
        "'Histórico do contrato. Somente inserção, mesma proteção da auditoria.'"
    )
    op.execute(
        "COMMENT ON COLUMN refresh_tokens.token_hash IS "
        "'SHA-256 do token. O token em claro nunca é persistido.'"
    )
    op.execute(
        "COMMENT ON COLUMN notifications.dedupe_key IS "
        "'Chave natural do evento. O índice único aqui é o que torna a automação idempotente.'"
    )
    op.execute(
        "COMMENT ON COLUMN contracts.version IS "
        "'Trava otimista: o UPDATE inclui WHERE version = <lida> e falha se outra transação gravou antes.'"
    )


def downgrade() -> None:
    for tabela in ("audit_logs", "contract_events"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{tabela}_somente_insercao ON {tabela}")
    op.execute("DROP FUNCTION IF EXISTS recusar_alteracao_de_registro_imutavel()")
    op.execute("DROP INDEX IF EXISTS ix_clients_legal_name_trgm")
    op.execute("DROP INDEX IF EXISTS uq_users_email_lower")
    # A extensão `pg_trgm` não é removida: outros objetos do banco podem
    # depender dela, e derrubá-la num downgrade quebraria o que não é nosso.
