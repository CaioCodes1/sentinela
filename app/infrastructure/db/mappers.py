"""Mapeamento imperativo: liga as entidades puras às tabelas.

Este arquivo é a única costura entre domínio e persistência. Ele é importado uma
vez, no início do processo (`configure_mappers()` em `app/main.py` e no
`conftest.py`), e a partir daí as classes de `app/domain/entities.py` passam a
ser persistíveis — **sem que elas tenham sido alteradas**.

É a diferença prática entre "usamos Clean Architecture" e usar de fato: o
domínio não importa SQLAlchemy nem aqui nem em lugar nenhum. Quem depende é a
infraestrutura, que é o sentido certo da seta.
"""

from __future__ import annotations

from sqlalchemy.orm import registry, relationship

from app.domain.entities import (
    AuditLog,
    Client,
    Contract,
    ContractEvent,
    Installment,
    JobRun,
    Notification,
    RefreshToken,
    User,
)
from app.infrastructure.db import tables

mapper_registry = registry(metadata=tables.metadata)

_configured = False


def configure_mappers() -> None:
    """Idempotente de propósito.

    O SQLAlchemy recusa mapear a mesma classe duas vezes, e em teste este módulo
    é importado por vários arquivos. A guarda evita o erro obscuro
    `Class ... already has a primary mapper defined`.
    """
    global _configured
    if _configured:
        return

    mapper_registry.map_imperatively(User, tables.users)

    mapper_registry.map_imperatively(
        RefreshToken,
        tables.refresh_tokens,
        properties={
            "user": relationship(User, lazy="select"),
        },
    )

    mapper_registry.map_imperatively(Client, tables.clients)

    mapper_registry.map_imperatively(
        Contract,
        tables.contracts,
        properties={
            "client": relationship(
                Client,
                lazy="select",
                foreign_keys=[tables.contracts.c.client_id],
            ),
            "installments": relationship(
                Installment,
                lazy="select",
                # Parcela é parte do contrato, não entidade independente:
                # `delete-orphan` impede parcela órfã apontando para contrato
                # que não existe mais.
                cascade="all, delete-orphan",
                order_by=tables.installments.c.due_date,
            ),
            "events": relationship(
                ContractEvent,
                lazy="select",
                cascade="all, delete-orphan",
                order_by=tables.contract_events.c.occurred_at.desc(),
            ),
        },
        # Trava otimista gerenciada pela aplicação. O `Contract` já incrementa
        # `version` em cada mutação; `version_id_generator=False` diz ao ORM
        # para usar esse valor em vez de gerar o dele, e ainda assim incluir
        # `WHERE version = :anterior` no UPDATE. Se outra transação tiver
        # gravado no meio, zero linhas são afetadas e o SQLAlchemy levanta
        # `StaleDataError` — que a unidade de trabalho traduz para 409.
        version_id_col=tables.contracts.c.version,
        version_id_generator=False,
    )

    mapper_registry.map_imperatively(Installment, tables.installments)
    mapper_registry.map_imperatively(ContractEvent, tables.contract_events)
    mapper_registry.map_imperatively(Notification, tables.notifications)
    mapper_registry.map_imperatively(AuditLog, tables.audit_logs)
    mapper_registry.map_imperatively(JobRun, tables.job_runs)

    _configured = True


def is_configured() -> bool:
    return _configured
