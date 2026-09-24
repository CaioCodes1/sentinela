"""Repositório de refresh tokens."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session

from app.domain.entities import RefreshToken
from app.infrastructure.db import tables


class SqlRefreshTokenRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, token: RefreshToken) -> None:
        self._session.add(token)

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        statement = select(RefreshToken).where(tables.refresh_tokens.c.token_hash == token_hash)
        return self._session.execute(statement).scalar_one_or_none()

    def list_by_family(self, family_id: uuid.UUID) -> list[RefreshToken]:
        statement = select(RefreshToken).where(tables.refresh_tokens.c.family_id == family_id)
        return list(self._session.execute(statement).scalars().all())

    def revoke_family(self, family_id: uuid.UUID, *, now: datetime) -> int:
        """Revoga a família inteira — a resposta à detecção de reúso.

        É um `UPDATE` em massa, não um laço carregando objetos: a família pode
        ter dezenas de tokens e o que interessa é que a revogação seja atômica
        e imediata. O `synchronize_session=False` evita que o SQLAlchemy tente
        reconciliar objetos já carregados; nada mais nesta transação os lê.
        """
        statement = (
            update(tables.refresh_tokens)
            .where(
                tables.refresh_tokens.c.family_id == family_id,
                tables.refresh_tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        resultado = cast(CursorResult[Any], self._session.execute(statement))
        return int(resultado.rowcount or 0)

    def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        """Usado no logout global e ao trocar a senha."""
        statement = (
            update(tables.refresh_tokens)
            .where(
                tables.refresh_tokens.c.user_id == user_id,
                tables.refresh_tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        resultado = cast(CursorResult[Any], self._session.execute(statement))
        return int(resultado.rowcount or 0)

    def purge_expired(self, *, before: datetime) -> int:
        """Faxina de tokens já vencidos.

        Sem ela, a tabela cresce para sempre: um usuário ativo gera um token
        novo a cada renovação de acesso. Manter registro de sessão vencida não
        serve à segurança — quem precisa de rastro histórico é a auditoria,
        que é outra tabela e tem retenção própria.
        """
        statement = delete(tables.refresh_tokens).where(tables.refresh_tokens.c.expires_at < before)
        resultado = cast(CursorResult[Any], self._session.execute(statement))
        return int(resultado.rowcount or 0)
