"""Repositório de contratos."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.domain.entities import Contract
from app.domain.enums import ContractStatus
from app.infrastructure.db import tables
from app.infrastructure.repositories._helpers import paginate

OPEN_STATUSES = (ContractStatus.ACTIVE, ContractStatus.EXPIRING)


class SqlContractRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, contract: Contract) -> None:
        self._session.add(contract)

    def get(self, contract_id: uuid.UUID) -> Contract | None:
        return self._session.get(Contract, contract_id)

    def get_by_number(self, number: str) -> Contract | None:
        statement = select(Contract).where(tables.contracts.c.number == number.upper())
        return self._session.execute(statement).scalar_one_or_none()

    def search(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: ContractStatus | None = None,
        expiring_until: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Contract], int]:
        statement = select(Contract)
        if client_id is not None:
            statement = statement.where(tables.contracts.c.client_id == client_id)
        if status is not None:
            statement = statement.where(tables.contracts.c.status == status)
        if expiring_until is not None:
            statement = statement.where(
                tables.contracts.c.end_date <= expiring_until,
                tables.contracts.c.status.in_(OPEN_STATUSES),
            )
        statement = statement.order_by(tables.contracts.c.end_date)
        rows, total = paginate(self._session, statement, limit=limit, offset=offset)
        return list(rows), total

    def list_open_contracts(self) -> list[Contract]:
        """Carrega os contratos vigentes **com o cliente junto**.

        O `selectinload` é o que separa um job saudável de um job patológico.
        Sem ele, montar o e-mail de cada contrato acessa `contract.client` e
        dispara um SELECT por contrato: com 5.000 contratos, são 5.001
        consultas — o clássico N+1. O `selectinload` resolve em duas, e ao
        contrário do `joinedload` não multiplica linhas quando houver coleção.
        """
        statement = (
            select(Contract)
            .where(tables.contracts.c.status.in_(OPEN_STATUSES))
            # `Contract.client` é instalado pelo mapeamento imperativo em tempo
            # de execução, então o mypy não o vê na classe. O ignore é
            # específico para isso, e não um silenciamento genérico.
            .options(selectinload(Contract.client))  # type: ignore[attr-defined]
            .order_by(tables.contracts.c.end_date)
        )
        return list(self._session.execute(statement).scalars().all())

    def count_open_by_client(self, client_id: uuid.UUID) -> int:
        """Usado para impedir a desativação de cliente com contrato vigente."""
        statement = (
            select(func.count())
            .select_from(tables.contracts)
            .where(
                tables.contracts.c.client_id == client_id,
                tables.contracts.c.status.in_(OPEN_STATUSES),
            )
        )
        return int(self._session.execute(statement).scalar_one())
