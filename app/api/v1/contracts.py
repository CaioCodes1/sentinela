"""Rotas de contratos e mensalidades."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, get_contract_service, require
from app.api.schemas.common import Page, ProblemDetail
from app.api.schemas.contract import (
    CancelContractRequest,
    ContractEventResponse,
    ContractResponse,
    CreateContractRequest,
    InstallmentResponse,
    RenewContractRequest,
    UpdateContractRequest,
)
from app.core.permissions import Permission
from app.domain.enums import ContractStatus
from app.services.contract_service import ContractService

router = APIRouter(prefix="/contracts", tags=["Contratos"])

ContractServiceDep = Annotated[ContractService, Depends(get_contract_service)]

ERROS: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetail, "description": "Não autenticado"},
    403: {"model": ProblemDetail, "description": "Perfil sem permissão"},
    404: {"model": ProblemDetail, "description": "Contrato não encontrado"},
    409: {"model": ProblemDetail, "description": "Conflito de versão ou número duplicado"},
    422: {"model": ProblemDetail, "description": "Regra de negócio violada"},
}


@router.post(
    "",
    response_model=ContractResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.CONTRACT_CREATE))],
    responses=ERROS,
    summary="Cria contrato e gera as mensalidades",
)
def create_contract(
    payload: CreateContractRequest, service: ContractServiceDep, user: CurrentUser
) -> ContractResponse:
    """Cria o contrato e, na **mesma transação**, gera uma parcela por
    competência coberta pela vigência.

    Se a geração de parcelas falhar, o contrato não é criado. A alternativa —
    criar o contrato e gerar parcelas depois — produziria contratos sem
    cobrança, que só aparecem quando o financeiro fecha o mês.
    """
    contract = service.create(
        client_id=payload.client_id,
        number=payload.number,
        monthly_amount=payload.monthly_amount,
        start_date=payload.start_date,
        end_date=payload.end_date,
        due_day=payload.due_day,
        description=payload.description,
        auto_renew=payload.auto_renew,
        renewal_term_months=payload.renewal_term_months,
        created_by_id=user.id,
    )
    return ContractResponse.model_validate(contract)


@router.get(
    "",
    response_model=Page[ContractResponse],
    dependencies=[Depends(require(Permission.CONTRACT_READ))],
    responses=ERROS,
    summary="Lista contratos com filtros",
)
def list_contracts(
    service: ContractServiceDep,
    client_id: Annotated[uuid.UUID | None, Query()] = None,
    contract_status: Annotated[ContractStatus | None, Query(alias="status")] = None,
    expiring_until: Annotated[
        date | None, Query(description="Vigentes com término até esta data")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ContractResponse]:
    """O filtro `expiring_until` é o que uma tela de "a vencer" consome."""
    items, total = service.search(
        client_id=client_id,
        status=contract_status,
        expiring_until=expiring_until,
        limit=limit,
        offset=offset,
    )
    return Page[ContractResponse](
        items=[ContractResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{contract_id}",
    response_model=ContractResponse,
    dependencies=[Depends(require(Permission.CONTRACT_READ))],
    responses=ERROS,
    summary="Consulta contrato",
)
def get_contract(contract_id: uuid.UUID, service: ContractServiceDep) -> ContractResponse:
    return ContractResponse.model_validate(service.get(contract_id))


@router.patch(
    "/{contract_id}",
    response_model=ContractResponse,
    dependencies=[Depends(require(Permission.CONTRACT_UPDATE))],
    responses=ERROS,
    summary="Altera contrato",
)
def update_contract(
    contract_id: uuid.UUID, payload: UpdateContractRequest, service: ContractServiceDep
) -> ContractResponse:
    """Alteração parcial com trava otimista opcional.

    Envie `expected_version` com a versão que você leu: se outro operador tiver
    gravado nesse intervalo, a resposta é 409 em vez de sobrescrever em
    silêncio a alteração dele.
    """
    contract = service.update(
        contract_id,
        expected_version=payload.expected_version,
        description=payload.description,
        monthly_amount=payload.monthly_amount,
        end_date=payload.end_date,
        due_day=payload.due_day,
        auto_renew=payload.auto_renew,
        renewal_term_months=payload.renewal_term_months,
    )
    return ContractResponse.model_validate(contract)


@router.post(
    "/{contract_id}/cancel",
    response_model=ContractResponse,
    dependencies=[Depends(require(Permission.CONTRACT_CANCEL))],
    responses=ERROS,
    summary="Cancela contrato",
)
def cancel_contract(
    contract_id: uuid.UUID, payload: CancelContractRequest, service: ContractServiceDep
) -> ContractResponse:
    """Cancelamento é **terminal**: o contrato não volta a ficar ativo nem é
    renovado depois. O motivo é obrigatório e fica no histórico."""
    return ContractResponse.model_validate(service.cancel(contract_id, reason=payload.reason))


@router.post(
    "/{contract_id}/renew",
    response_model=ContractResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.CONTRACT_RENEW))],
    responses=ERROS,
    summary="Renova o contrato criando o sucessor",
)
def renew_contract(
    contract_id: uuid.UUID,
    payload: RenewContractRequest,
    service: ContractServiceDep,
    user: CurrentUser,
) -> ContractResponse:
    """Devolve o **contrato novo**, não o antigo.

    A renovação cria um sucessor com vigência começando no dia seguinte ao
    término do anterior; o original vai para RENEWED apontando para ele. Por
    isso a resposta é 201, e não 200: um recurso novo foi criado.
    """
    successor = service.renew(
        contract_id,
        term_months=payload.term_months,
        adjustment_percent=payload.adjustment_percent,
        created_by_id=user.id,
    )
    return ContractResponse.model_validate(successor)


@router.get(
    "/{contract_id}/history",
    response_model=list[ContractEventResponse],
    dependencies=[Depends(require(Permission.CONTRACT_READ))],
    responses=ERROS,
    summary="Histórico do contrato",
)
def contract_history(
    contract_id: uuid.UUID,
    service: ContractServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ContractEventResponse]:
    """Linha do tempo: criação, alterações (com o antes e o depois), renovação,
    cancelamento, vencimento e quitação de parcela."""
    return [
        ContractEventResponse.model_validate(event)
        for event in service.history(contract_id, limit=limit)
    ]


@router.get(
    "/{contract_id}/installments",
    response_model=list[InstallmentResponse],
    dependencies=[Depends(require(Permission.INSTALLMENT_READ))],
    responses=ERROS,
    summary="Mensalidades do contrato",
)
def contract_installments(
    contract_id: uuid.UUID, service: ContractServiceDep
) -> list[InstallmentResponse]:
    return [InstallmentResponse.model_validate(item) for item in service.installments(contract_id)]


@router.post(
    "/installments/{installment_id}/settle",
    response_model=InstallmentResponse,
    dependencies=[Depends(require(Permission.INSTALLMENT_SETTLE))],
    responses=ERROS,
    summary="Registra a quitação de uma mensalidade",
)
def settle_installment(
    installment_id: uuid.UUID, service: ContractServiceDep
) -> InstallmentResponse:
    return InstallmentResponse.model_validate(service.settle_installment(installment_id))
