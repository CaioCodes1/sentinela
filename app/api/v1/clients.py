"""Rotas de clientes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, get_client_service, require
from app.api.schemas.client import (
    ClientResponse,
    CreateClientRequest,
    DeactivateClientRequest,
    UpdateClientRequest,
)
from app.api.schemas.common import Page, ProblemDetail
from app.core.permissions import Permission
from app.services.client_service import ClientService

router = APIRouter(prefix="/clients", tags=["Clientes"])

ClientServiceDep = Annotated[ClientService, Depends(get_client_service)]

ERROS: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetail, "description": "Não autenticado"},
    403: {"model": ProblemDetail, "description": "Perfil sem permissão"},
    404: {"model": ProblemDetail, "description": "Cliente não encontrado"},
}


@router.post(
    "",
    response_model=ClientResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.CLIENT_CREATE))],
    responses={**ERROS, 409: {"model": ProblemDetail, "description": "Documento já cadastrado"}},
    summary="Cria cliente",
)
def create_client(
    payload: CreateClientRequest, service: ClientServiceDep, user: CurrentUser
) -> ClientResponse:
    """Cadastra pessoa física ou jurídica.

    O documento é validado pelo dígito verificador e gravado só com os dígitos,
    o que impede o mesmo CNPJ entrar duas vezes com pontuação diferente.
    """
    client = service.create(
        tax_id=payload.tax_id,
        legal_name=payload.legal_name,
        email=payload.email,
        phone=payload.phone,
        trade_name=payload.trade_name,
        created_by_id=user.id,
    )
    return ClientResponse.model_validate(client)


@router.get(
    "",
    response_model=Page[ClientResponse],
    dependencies=[Depends(require(Permission.CLIENT_READ))],
    responses=ERROS,
    summary="Lista clientes com busca e paginação",
)
def list_clients(
    service: ClientServiceDep,
    term: Annotated[str | None, Query(max_length=120, description="Nome ou documento")] = None,
    only_active: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ClientResponse]:
    items, total = service.search(term=term, only_active=only_active, limit=limit, offset=offset)
    return Page[ClientResponse](
        items=[ClientResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{client_id}",
    response_model=ClientResponse,
    dependencies=[Depends(require(Permission.CLIENT_READ))],
    responses=ERROS,
    summary="Consulta cliente",
)
def get_client(client_id: uuid.UUID, service: ClientServiceDep) -> ClientResponse:
    return ClientResponse.model_validate(service.get(client_id))


@router.patch(
    "/{client_id}",
    response_model=ClientResponse,
    dependencies=[Depends(require(Permission.CLIENT_UPDATE))],
    responses=ERROS,
    summary="Altera cliente",
)
def update_client(
    client_id: uuid.UUID, payload: UpdateClientRequest, service: ClientServiceDep
) -> ClientResponse:
    """Alteração parcial. O documento não é alterável — ver o schema."""
    client = service.update(
        client_id,
        legal_name=payload.legal_name,
        trade_name=payload.trade_name,
        email=payload.email,
        phone=payload.phone,
    )
    return ClientResponse.model_validate(client)


@router.post(
    "/{client_id}/deactivate",
    response_model=ClientResponse,
    dependencies=[Depends(require(Permission.CLIENT_DEACTIVATE))],
    responses={
        **ERROS,
        422: {"model": ProblemDetail, "description": "Cliente possui contrato vigente"},
    },
    summary="Desativa cliente",
)
def deactivate_client(
    client_id: uuid.UUID, payload: DeactivateClientRequest, service: ClientServiceDep
) -> ClientResponse:
    """Desativa em vez de apagar.

    É `POST /{id}/deactivate` e não `DELETE /{id}` de propósito: o verbo
    descreve o que de fato acontece. `DELETE` sugeriria remoção, e ninguém deve
    descobrir só lendo o log que o "delete" desta API na verdade preserva tudo.

    Recusa com 422 se houver contrato vigente — desativar o titular de um
    contrato em vigor deixaria a cobrança rodando para um cadastro inativo.
    """
    client = service.deactivate(client_id, reason=payload.reason)
    return ClientResponse.model_validate(client)
