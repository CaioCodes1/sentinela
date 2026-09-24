"""Repositório de mensalidades."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.domain.entities import Installment
from app.domain.enums import InstallmentStatus
from app.infrastructure.db import tables


class SqlInstallmentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, installment: Installment) -> None:
        self._session.add(installment)

    def get(self, installment_id: uuid.UUID) -> Installment | None:
        return self._session.get(Installment, installment_id)

    def add_many_ignoring_duplicates(self, installments: list[Installment]) -> int:
        """Insere em lote, ignorando o que já existe.

        Implementado com `INSERT ... ON CONFLICT DO NOTHING` por um motivo
        específico do PostgreSQL, e não por elegância: **a primeira instrução
        que falha aborta a transação inteira**. Um `try/except IntegrityError`
        em volta de cada insert parece funcionar — o `except` executa e o
        código segue — mas todo comando seguinte na mesma transação é recusado
        com `current transaction is aborted, commands ignored until end of
        transaction block`.

        O detalhe cruel é que o teste não pega: com **uma** parcela duplicada e
        nada depois dela, o fluxo nunca chega ao comando seguinte e tudo parece
        certo. Só quebra em produção, no segundo contrato do lote.

        A deduplicação precisa acontecer **dentro** do comando. Aqui ela se
        apoia em `uq_installments_contract_id_competence`, e é o que torna o
        job idempotente: rodar duas vezes no mesmo dia não gera cobrança dupla.
        """
        if not installments:
            return 0

        values = [
            {
                "id": item.id,
                "contract_id": item.contract_id,
                "competence": item.competence,
                "due_date": item.due_date,
                "amount": item.amount,
                "status": item.status,
                "paid_at": item.paid_at,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in installments
        ]
        # `RETURNING` em vez de `rowcount`: o psycopg3 devolve -1 ("não sei")
        # para este comando, o que faria a contagem de parcelas geradas ser
        # sempre -1 — número que vai para o histórico do contrato e para as
        # estatísticas do job. Contar as linhas devolvidas é exato.
        statement = (
            pg_insert(tables.installments)
            .values(values)
            .on_conflict_do_nothing(index_elements=["contract_id", "competence"])
            .returning(tables.installments.c.id)
        )
        return len(self._session.execute(statement).fetchall())

    def list_by_contract(self, contract_id: uuid.UUID) -> list[Installment]:
        statement = (
            select(Installment)
            .where(tables.installments.c.contract_id == contract_id)
            .order_by(tables.installments.c.due_date)
        )
        return list(self._session.execute(statement).scalars().all())

    def list_overdue(self, *, reference: date) -> list[Installment]:
        """Parcelas cujo vencimento já passou e que continuam em aberto.

        `<` e não `<=`: no próprio dia do vencimento a parcela ainda está no
        prazo. Marcá-la como atrasada às 3h da manhã do dia do vencimento
        mandaria cobrança de inadimplência para quem tem o dia inteiro para
        pagar.
        """
        statement = (
            select(Installment)
            .where(
                tables.installments.c.due_date < reference,
                tables.installments.c.status.in_(
                    (InstallmentStatus.PENDING, InstallmentStatus.OVERDUE)
                ),
            )
            .order_by(tables.installments.c.due_date)
        )
        return list(self._session.execute(statement).scalars().all())
