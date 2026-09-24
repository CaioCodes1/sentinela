"""Repositório do histórico de contratos (append-only, como a auditoria)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities import ContractEvent
from app.infrastructure.db import tables


class SqlContractEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: ContractEvent) -> None:
        self._session.add(event)

    def list_by_contract(self, contract_id: uuid.UUID, *, limit: int = 100) -> list[ContractEvent]:
        statement = (
            select(ContractEvent)
            .where(tables.contract_events.c.contract_id == contract_id)
            .order_by(tables.contract_events.c.occurred_at.desc())
            .limit(min(limit, 500))
        )
        return list(self._session.execute(statement).scalars().all())
