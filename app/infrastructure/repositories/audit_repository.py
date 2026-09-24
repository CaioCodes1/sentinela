"""Repositório de auditoria.

Repare no que **não** existe aqui: `update` e `delete`. Não é esquecimento — a
tabela `audit_logs` é protegida por gatilho no banco, e qualquer tentativa de
alterar ou remover uma linha é recusada com exceção. Expor um método que o
banco vai rejeitar só serviria para alguém descobrir isso em produção.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities import AuditLog
from app.infrastructure.db import tables
from app.infrastructure.repositories._helpers import paginate


class SqlAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entry: AuditLog) -> None:
        self._session.add(entry)

    def search(
        self,
        *,
        actor_user_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        statement = select(AuditLog)
        if actor_user_id is not None:
            statement = statement.where(tables.audit_logs.c.actor_user_id == actor_user_id)
        if action:
            statement = statement.where(tables.audit_logs.c.action == action)
        if resource_type:
            statement = statement.where(tables.audit_logs.c.resource_type == resource_type)
        if resource_id:
            statement = statement.where(tables.audit_logs.c.resource_id == resource_id)
        if occurred_from is not None:
            statement = statement.where(tables.audit_logs.c.occurred_at >= occurred_from)
        if occurred_to is not None:
            statement = statement.where(tables.audit_logs.c.occurred_at <= occurred_to)

        # Decrescente, casando com `ix_audit_logs_occurred_at (occurred_at DESC)`:
        # a pergunta do auditor é quase sempre "o que aconteceu por último".
        statement = statement.order_by(tables.audit_logs.c.occurred_at.desc())
        rows, total = paginate(self._session, statement, limit=limit, offset=offset)
        return list(rows), total
