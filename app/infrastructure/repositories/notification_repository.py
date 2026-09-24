"""Repositório de notificações."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.domain.entities import Notification
from app.domain.enums import NotificationStatus
from app.infrastructure.db import tables
from app.infrastructure.repositories._helpers import paginate

DISPATCHABLE = (NotificationStatus.PENDING, NotificationStatus.FAILED)


class SqlNotificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(self, notification: Notification) -> bool:
        """Insere só se a `dedupe_key` for inédita. Devolve `True` se inseriu.

        A garantia de "uma notificação por evento" mora no índice único da
        coluna `dedupe_key`, não neste método. É uma diferença que importa: a
        checagem "já existe?" seguida de insert tem uma janela entre as duas
        operações, e duas réplicas rodando o job ao mesmo tempo passam as duas
        pela checagem antes de qualquer uma inserir. O banco não tem essa
        janela.

        **O `RETURNING` não é enfeite.** A forma óbvia de saber se houve
        inserção seria `bool(resultado.rowcount)` — e ela está errada com o
        psycopg3: nestes `INSERT ... ON CONFLICT` o driver devolve `rowcount`
        **-1** ("desconhecido"), tanto na inserção quanto no conflito. Como
        `bool(-1)` é `True`, o método respondia "inseri" sempre. O efeito era
        silencioso e ruim: o banco continuava recusando a duplicata, então
        nenhuma notificação repetida era criada, mas o job **relatava** alertas
        gerados a cada execução — o número que vai para o painel e para o
        `job_runs` passava a mentir.

        Com `RETURNING id`, a resposta vem do próprio comando: linha devolvida
        significa linha inserida, sem depender de como o driver conta.

        Este defeito passou por toda a suíte unitária, porque o dublê em
        memória implementava a semântica correta. Só um banco de verdade
        mostrou a diferença.
        """
        statement = (
            pg_insert(tables.notifications)
            .values(
                id=notification.id,
                contract_id=notification.contract_id,
                notification_type=notification.notification_type,
                channel=notification.channel,
                recipient=notification.recipient,
                subject=notification.subject,
                body=notification.body,
                dedupe_key=notification.dedupe_key,
                status=notification.status,
                attempts=notification.attempts,
                created_at=notification.created_at,
                updated_at=notification.updated_at,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(tables.notifications.c.id)
        )
        return self._session.execute(statement).first() is not None

    def get(self, notification_id: uuid.UUID) -> Notification | None:
        return self._session.get(Notification, notification_id)

    def list_dispatchable(self, *, limit: int = 200) -> list[Notification]:
        """Fila de envio: mais antigas primeiro.

        A ordenação é **crescente** por `created_at`, de propósito. Com ordem
        decrescente e um lote menor que a fila, as notificações mais antigas
        nunca chegam ao topo: a cada execução entram novas na frente e as
        antigas envelhecem para sempre — inanição de fila, com o painel
        mostrando "todas as execuções com sucesso" o tempo inteiro.
        """
        statement = (
            select(Notification)
            .where(tables.notifications.c.status.in_(DISPATCHABLE))
            .order_by(tables.notifications.c.created_at.asc())
            .limit(min(limit, 500))
        )
        return list(self._session.execute(statement).scalars().all())

    def search(
        self,
        *,
        contract_id: uuid.UUID | None = None,
        status: NotificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Notification], int]:
        statement = select(Notification)
        if contract_id is not None:
            statement = statement.where(tables.notifications.c.contract_id == contract_id)
        if status is not None:
            statement = statement.where(tables.notifications.c.status == status)
        statement = statement.order_by(tables.notifications.c.created_at.desc())
        rows, total = paginate(self._session, statement, limit=limit, offset=offset)
        return list(rows), total
