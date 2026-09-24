"""Repositório de usuários."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.entities import User
from app.infrastructure.db import tables


class SqlUserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> None:
        self._session.add(user)

    def get(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        """Busca sem diferenciar maiúsculas.

        A comparação é `lower(email) = lower(:email)` para bater com o índice
        único funcional criado na migration. Se aqui fosse `==` direto e o
        índice fosse sobre `lower()`, o PostgreSQL ignoraria o índice e faria
        varredura sequencial na tabela de usuários **a cada tentativa de
        login** — que é justamente o endpoint que um atacante martela.
        """
        statement = select(User).where(func.lower(tables.users.c.email) == func.lower(email))
        return self._session.execute(statement).scalar_one_or_none()

    def list_all(self) -> list[User]:
        statement = select(User).order_by(tables.users.c.created_at)
        return list(self._session.execute(statement).scalars().all())
