"""Consulta à trilha de auditoria.

Só leitura, e só para quem tem `audit:read` — hoje, apenas o perfil AUDITOR e o
ADMIN. Não existe rota de alteração nem de remoção: a tabela é protegida por
gatilho no banco, e expor um endpoint que o banco vai recusar seria pior do que
não ter endpoint nenhum.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import UowDep, require
from app.api.schemas.audit import AuditLogResponse
from app.api.schemas.common import Page, ProblemDetail
from app.core.permissions import Permission
from app.domain.enums import AuditAction

router = APIRouter(prefix="/audit", tags=["Auditoria"])


@router.get(
    "/logs",
    response_model=Page[AuditLogResponse],
    dependencies=[Depends(require(Permission.AUDIT_READ))],
    responses={
        401: {"model": ProblemDetail, "description": "Não autenticado"},
        403: {"model": ProblemDetail, "description": "Perfil sem permissão de auditoria"},
    },
    summary="Consulta a trilha de auditoria",
)
def list_audit_logs(
    uow: UowDep,
    actor_user_id: Annotated[uuid.UUID | None, Query(description="Filtra por autor")] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    resource_type: Annotated[str | None, Query(max_length=40)] = None,
    resource_id: Annotated[str | None, Query(max_length=64)] = None,
    occurred_from: Annotated[datetime | None, Query()] = None,
    occurred_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditLogResponse]:
    """Cada linha responde às cinco perguntas de uma investigação: **quem**
    (`actor_email`, `actor_role`, `actor_user_id`), **o quê** (`action`),
    **quando** (`occurred_at`), **de onde** (`ip_address`) e **sobre qual
    recurso** (`resource_type` + `resource_id`).

    O `request_id` amarra a linha às entradas de log daquela mesma requisição.
    """
    items, total = uow.audit.search(
        actor_user_id=actor_user_id,
        action=action.value if action else None,
        resource_type=resource_type,
        resource_id=resource_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        limit=limit,
        offset=offset,
    )
    return Page[AuditLogResponse](
        items=[AuditLogResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/resources/{resource_type}/{resource_id}",
    response_model=Page[AuditLogResponse],
    dependencies=[Depends(require(Permission.AUDIT_READ))],
    summary="Trilha completa de um recurso específico",
)
def resource_trail(
    resource_type: str,
    resource_id: str,
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditLogResponse]:
    """Atalho para "tudo o que já aconteceu com este contrato/cliente"."""
    items, total = uow.audit.search(
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
        offset=offset,
    )
    return Page[AuditLogResponse](
        items=[AuditLogResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
