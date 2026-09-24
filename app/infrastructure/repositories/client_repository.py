"""Repositório de clientes."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.domain.entities import Client
from app.domain.enums import ClientStatus
from app.infrastructure.db import tables
from app.infrastructure.repositories._helpers import escape_like, paginate


class SqlClientRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, client: Client) -> None:
        self._session.add(client)

    def get(self, client_id: uuid.UUID) -> Client | None:
        return self._session.get(Client, client_id)

    def get_by_tax_id(self, tax_id_digits: str) -> Client | None:
        statement = select(Client).where(tables.clients.c.tax_id == tax_id_digits)
        return self._session.execute(statement).scalar_one_or_none()

    def search(
        self,
        *,
        term: str | None = None,
        only_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Client], int]:
        statement = select(Client)

        if term:
            # O termo entra como **parâmetro ligado**, nunca concatenado na
            # string de SQL. Quem monta a consulta é o SQLAlchemy; o valor
            # viaja separado, e é por isso que `'; DROP TABLE clients; --`
            # é apenas um cliente que não existe.
            pattern = f"%{escape_like(term.strip())}%"
            statement = statement.where(
                or_(
                    tables.clients.c.legal_name.ilike(pattern, escape="\\"),
                    tables.clients.c.trade_name.ilike(pattern, escape="\\"),
                    # Documento é buscado por prefixo de dígitos: o usuário
                    # pode digitar com pontuação, então comparamos só o que
                    # sobrou de numérico.
                    tables.clients.c.tax_id.like(
                        f"{escape_like(''.join(c for c in term if c.isdigit()))}%",
                        escape="\\",
                    )
                    if any(c.isdigit() for c in term)
                    else tables.clients.c.legal_name.ilike(pattern, escape="\\"),
                )
            )

        if only_active is True:
            statement = statement.where(tables.clients.c.status == ClientStatus.ACTIVE)
        elif only_active is False:
            statement = statement.where(tables.clients.c.status == ClientStatus.INACTIVE)

        statement = statement.order_by(tables.clients.c.legal_name)
        rows, total = paginate(self._session, statement, limit=limit, offset=offset)
        return list(rows), total
