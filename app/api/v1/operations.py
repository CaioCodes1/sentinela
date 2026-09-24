"""Rotas operacionais: notificações, execuções da automação e matriz de RBAC."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from app.api.deps import (
    UowDep,
    get_automation_service,
    get_notification_service,
    require,
)
from app.api.schemas.common import Page, ProblemDetail
from app.api.schemas.contract import JobRunResponse, NotificationResponse
from app.core.permissions import Permission, describe_matrix
from app.domain.enums import NotificationStatus
from app.services.automation_service import AutomationService
from app.services.notification_service import NotificationService

router = APIRouter(tags=["Operação"])

NotificationDep = Annotated[NotificationService, Depends(get_notification_service)]
AutomationDep = Annotated[AutomationService, Depends(get_automation_service)]


@router.get(
    "/notifications",
    response_model=Page[NotificationResponse],
    dependencies=[Depends(require(Permission.NOTIFICATION_READ))],
    summary="Lista notificações geradas pela automação",
)
def list_notifications(
    service: NotificationDep,
    contract_id: Annotated[uuid.UUID | None, Query()] = None,
    notification_status: Annotated[NotificationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[NotificationResponse]:
    """Filtre por `status=DEAD_LETTER` para achar o que esgotou as tentativas
    e precisa de ação humana."""
    items, total = service.search(
        contract_id=contract_id, status=notification_status, limit=limit, offset=offset
    )
    return Page[NotificationResponse](
        items=[NotificationResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/notifications/{notification_id}/retry",
    response_model=NotificationResponse,
    dependencies=[Depends(require(Permission.NOTIFICATION_RETRY))],
    responses={404: {"model": ProblemDetail}, 422: {"model": ProblemDetail}},
    summary="Recoloca uma notificação na fila de envio",
)
def retry_notification(
    notification_id: uuid.UUID, service: NotificationDep
) -> NotificationResponse:
    """Para o que foi para a fila morta depois de esgotar as tentativas
    automáticas. Zera o contador; o envio acontece no próximo ciclo."""
    return NotificationResponse.model_validate(service.retry(notification_id))


@router.get(
    "/jobs/runs",
    response_model=list[JobRunResponse],
    dependencies=[Depends(require(Permission.JOB_READ))],
    summary="Últimas execuções da automação",
)
def list_job_runs(
    uow: UowDep,
    job_name: Annotated[str | None, Query(max_length=60)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[JobRunResponse]:
    """Responde "o job rodou hoje, quanto tempo levou e o que fez".

    Sem esta tabela, a única evidência de execução seria o log — que expira e
    que ninguém consulta até alguém reclamar de um aviso que não chegou.
    """
    return [
        JobRunResponse(
            id=run.id,
            job_name=run.job_name,
            status=run.status.value,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
            stats=run.stats,
            error_message=run.error_message,
        )
        for run in uow.job_runs.list_recent(job_name=job_name, limit=limit)
    ]


@router.post(
    "/jobs/daily-check/run",
    dependencies=[Depends(require(Permission.JOB_RUN))],
    summary="Dispara a verificação diária sob demanda (somente ADMIN)",
)
def run_daily_check(
    service: AutomationDep,
    reference: Annotated[
        date | None,
        Body(
            embed=True,
            description="Data de referência; o padrão é hoje. Útil para reprocessar um dia.",
        ),
    ] = None,
) -> dict[str, Any]:
    """Execução manual do job.

    Restrito ao ADMIN porque **envia mensagem de verdade para cliente de
    verdade**. É idempotente: reprocessar um dia já processado não gera aviso
    duplicado, porque cada notificação tem chave única no banco.

    Se outra instância já estiver executando, devolve `SKIPPED` — comportamento
    correto, não erro.
    """
    return service.run_daily(reference=reference)


@router.get(
    "/admin/permissions",
    dependencies=[Depends(require(Permission.USER_READ))],
    summary="Matriz de permissões por perfil",
)
def permission_matrix() -> dict[str, list[str]]:
    """Publica a matriz **viva** do RBAC, lida do código em tempo de execução.

    Um documento descrevendo permissões envelhece no primeiro endpoint novo.
    Este endpoint não tem como divergir: ele é a mesma estrutura que autoriza as
    requisições.
    """
    return describe_matrix()
