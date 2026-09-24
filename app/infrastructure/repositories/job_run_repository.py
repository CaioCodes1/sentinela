"""Repositório das execuções da automação."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.entities import JobRun
from app.infrastructure.db import tables


class SqlJobRunRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: JobRun) -> None:
        self._session.add(run)

    def list_recent(self, *, job_name: str | None = None, limit: int = 20) -> list[JobRun]:
        """Responde "o job rodou hoje? quanto tempo levou? o que fez?".

        Sem esta tabela, a única prova de que a automação executou é o log — que
        expira, que pode não ter sido coletado naquela noite, e que ninguém
        consulta até o cliente reclamar de um aviso que não chegou.
        """
        statement = select(JobRun)
        if job_name:
            statement = statement.where(tables.job_runs.c.job_name == job_name)
        statement = statement.order_by(tables.job_runs.c.started_at.desc()).limit(min(limit, 200))
        return list(self._session.execute(statement).scalars().all())
