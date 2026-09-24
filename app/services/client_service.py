"""Casos de uso de clientes."""

from __future__ import annotations

import uuid

from app.domain.entities import Client
from app.domain.enums import AuditAction
from app.domain.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.domain.ports.uow import UnitOfWork
from app.domain.value_objects import EmailAddress, TaxId
from app.services.audit_service import AuditService


class ClientService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow
        self._audit = AuditService(uow)

    def create(
        self,
        *,
        tax_id: str,
        legal_name: str,
        email: str,
        phone: str | None = None,
        trade_name: str | None = None,
        created_by_id: uuid.UUID | None = None,
    ) -> Client:
        parsed_tax_id = TaxId.parse(tax_id)

        # Checagem prévia para dar uma mensagem útil ("já existe cliente com
        # este CNPJ") em vez do 409 genérico do índice único. Não substitui o
        # índice: entre esta consulta e o insert existe uma janela de corrida,
        # e quem fecha essa janela é a restrição no banco.
        if self._uow.clients.get_by_tax_id(parsed_tax_id.digits) is not None:
            raise ConflictError(
                "já existe cliente com este documento",
                details={"documento": parsed_tax_id.masked()},
            )

        client = Client(
            tax_id=parsed_tax_id,
            legal_name=legal_name,
            email=EmailAddress.parse(email),
            phone=phone,
            trade_name=trade_name,
            created_by_id=created_by_id,
        )
        self._uow.clients.add(client)
        self._audit.record(
            action=AuditAction.CLIENT_CREATED,
            resource_type="client",
            resource_id=client.id,
            # Só o documento mascarado entra na trilha.
            details={"documento": parsed_tax_id.masked(), "razao_social": client.legal_name},
        )
        self._uow.commit()
        return client

    def get(self, client_id: uuid.UUID) -> Client:
        client = self._uow.clients.get(client_id)
        if client is None:
            raise NotFoundError("cliente não encontrado")
        return client

    def search(
        self,
        *,
        term: str | None = None,
        only_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Client], int]:
        return self._uow.clients.search(
            term=term, only_active=only_active, limit=limit, offset=offset
        )

    def update(
        self,
        client_id: uuid.UUID,
        *,
        legal_name: str | None = None,
        trade_name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> Client:
        client = self.get(client_id)
        if not client.is_active:
            raise BusinessRuleError("cliente inativo não pode ser alterado")

        changes = client.update(
            legal_name=legal_name,
            trade_name=trade_name,
            email=EmailAddress.parse(email) if email else None,
            phone=phone,
        )
        if changes:
            self._audit.record(
                action=AuditAction.CLIENT_UPDATED,
                resource_type="client",
                resource_id=client.id,
                details={"alteracoes": changes},
            )
            self._uow.commit()
        return client

    def deactivate(self, client_id: uuid.UUID, *, reason: str | None = None) -> Client:
        """Desativa o cliente. **Nunca apaga.**

        Duas razões para não existir `DELETE`. A primeira é integridade
        referencial: contratos apontam para o cliente, e a chave estrangeira é
        `RESTRICT` justamente para que o banco recuse. A segunda é
        rastreabilidade — uma plataforma de contratos precisa responder "quem
        era o titular deste contrato de 2023", e um cliente apagado torna o
        histórico ilegível.
        """
        client = self.get(client_id)

        open_contracts = self._uow.contracts.count_open_by_client(client_id)
        if open_contracts:
            raise BusinessRuleError(
                "cliente possui contrato vigente e não pode ser desativado",
                details={"contratos_vigentes": open_contracts},
            )

        client.deactivate()
        self._audit.record(
            action=AuditAction.CLIENT_DEACTIVATED,
            resource_type="client",
            resource_id=client.id,
            details={"motivo": reason} if reason else None,
        )
        self._uow.commit()
        return client
