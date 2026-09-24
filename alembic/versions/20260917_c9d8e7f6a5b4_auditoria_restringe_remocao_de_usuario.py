"""auditoria passa a RESTRINGIR a remoção de usuário

Correção de uma colisão entre duas proteções que foram desenhadas separadamente
e que não podem coexistir como estavam. Descoberta por um teste de integração —
não havia como aparecer em teste unitário.

**O conflito.** A chave estrangeira `audit_logs.actor_user_id` foi criada com
`ON DELETE SET NULL`, com a intenção certa: apagar um usuário não pode levar
junto o registro do que ele fez. Só que `SET NULL` é implementado pelo
PostgreSQL como um `UPDATE` na tabela referenciada — e `audit_logs` tem um
gatilho `BEFORE UPDATE OR DELETE` que recusa qualquer alteração.

Resultado: `DELETE FROM users` falhava com
`a tabela audit_logs é somente-inserção: UPDATE não é permitido`. A remoção de
usuário era **impossível**, e a cláusula `ON DELETE SET NULL` era código morto:
uma regra escrita no schema que nunca poderia ser executada.

**A saída escolhida: `RESTRICT`.** É a que diz a verdade sobre o sistema.
Usuário, neste domínio, não é apagado — é desativado, exatamente como cliente.
Com `RESTRICT`, o banco recusa a remoção de quem tem trilha, em vez de fingir
que trataria o caso.

**As alternativas, e por que não.**

- *Afrouxar o gatilho* para aceitar o `UPDATE` da chave estrangeira: abriria a
  porta exata que a imutabilidade existe para fechar, e a exceção seria difícil
  de escrever sem virar brecha geral.
- *Deixar como estava*: manter uma regra que nunca dispara é pior que não ter
  regra, porque a próxima pessoa lê o schema e acredita nela.

Remoção definitiva de dados pessoais, quando exigida por lei, continua possível
pela via prevista: `TRUNCATE`, que não dispara gatilho de linha, aplicado dentro
de uma rotina de retenção com decisão registrada.

Revision ID: c9d8e7f6a5b4
Revises: b2a1f0c3d4e5
Create Date: 2026-09-17

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c9d8e7f6a5b4"
down_revision: str | None = "b2a1f0c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOME_FK = "fk_audit_logs_actor_user_id_users"


def upgrade() -> None:
    op.drop_constraint(NOME_FK, "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        NOME_FK,
        "audit_logs",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        "COMMENT ON COLUMN audit_logs.actor_user_id IS "
        "'RESTRICT: usuário com trilha de auditoria não pode ser removido, apenas "
        "desativado. SET NULL era inviável — o UPDATE implícito é recusado pelo "
        "gatilho de imutabilidade.'"
    )


def downgrade() -> None:
    op.drop_constraint(NOME_FK, "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        NOME_FK,
        "audit_logs",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
