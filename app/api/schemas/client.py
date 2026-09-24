"""Schemas de clientes."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.api.schemas.common import OutputModel, StrictModel, como_erro_de_campo
from app.domain.enums import ClientStatus
from app.domain.value_objects import EmailAddress, TaxId


class CreateClientRequest(StrictModel):
    tax_id: str = Field(
        max_length=20,
        description="CPF ou CNPJ, com ou sem pontuação",
        examples=["12.345.678/0001-95"],
    )
    legal_name: str = Field(min_length=3, max_length=180)
    trade_name: str | None = Field(default=None, max_length=180)
    email: str = Field(max_length=254)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("tax_id")
    @classmethod
    def _valid_tax_id(cls, value: str) -> str:
        # Valida aqui **e** no objeto de valor. A repetição é de propósito: o
        # schema devolve 422 com mensagem de campo, o que é a experiência certa
        # para quem chama a API; o objeto de valor protege os caminhos que não
        # passam por HTTP (job, CLI, importação).
        return como_erro_de_campo(lambda v: TaxId.parse(v).digits, value)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return como_erro_de_campo(lambda v: EmailAddress.parse(v).value, value)


class UpdateClientRequest(StrictModel):
    """Alteração parcial: todos os campos são opcionais.

    O documento (`tax_id`) **não** está aqui, e a ausência é deliberada. Trocar
    o CNPJ de um cliente não é editar um cadastro: é apontar todos os contratos
    históricos dele para outra pessoa jurídica. Quando isso precisa acontecer de
    verdade, o caminho é criar o novo cliente e migrar os contratos — com
    rastro.
    """

    legal_name: str | None = Field(default=None, min_length=3, max_length=180)
    trade_name: str | None = Field(default=None, max_length=180)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateClientRequest:
        if not any(
            value is not None
            for value in (self.legal_name, self.trade_name, self.email, self.phone)
        ):
            raise ValueError("informe ao menos um campo para alterar")
        return self

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str | None) -> str | None:
        if not value:
            return None
        return como_erro_de_campo(lambda v: EmailAddress.parse(v).value, value)


class DeactivateClientRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=300)


class ClientResponse(OutputModel):
    id: uuid.UUID
    tax_id: str = Field(description="Documento formatado")
    legal_name: str
    trade_name: str | None
    email: str
    phone: str | None
    status: ClientStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("tax_id", mode="before")
    @classmethod
    def _format_tax_id(cls, value: object) -> str:
        return value.formatted() if isinstance(value, TaxId) else str(value)

    @field_validator("email", mode="before")
    @classmethod
    def _stringify_email(cls, value: object) -> str:
        return str(value)
