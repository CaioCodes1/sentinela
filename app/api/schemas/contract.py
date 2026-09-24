"""Schemas de contratos, mensalidades, histórico e notificações."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from app.api.schemas.common import OutputModel, StrictModel, como_erro_de_campo
from app.domain.enums import (
    ContractStatus,
    InstallmentStatus,
    NotificationStatus,
    NotificationType,
)
from app.domain.value_objects import Money


class CreateContractRequest(StrictModel):
    client_id: uuid.UUID
    number: str = Field(min_length=3, max_length=40, examples=["CT-2026-001"])
    description: str | None = Field(default=None, max_length=500)
    # `Decimal` e não `float`: o Pydantic converteria `1234.56` em binário e o
    # valor chegaria como 1234.5599999999999 no serviço. `max_digits` e
    # `decimal_places` espelham a coluna NUMERIC(14,2).
    monthly_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    start_date: date
    end_date: date
    due_day: int = Field(ge=1, le=31, description="Dia de vencimento da mensalidade")
    auto_renew: bool = False
    renewal_term_months: int = Field(default=12, ge=1, le=120)

    @model_validator(mode="after")
    def _period_is_sane(self) -> CreateContractRequest:
        if self.end_date <= self.start_date:
            raise ValueError("end_date precisa ser posterior a start_date")
        return self

    @field_validator("monthly_amount")
    @classmethod
    def _two_decimals(cls, value: Decimal) -> Decimal:
        return como_erro_de_campo(lambda v: Money.parse(v).amount, value)


class UpdateContractRequest(StrictModel):
    """Alteração parcial.

    `start_date`, `client_id` e `number` não são alteráveis: os três definem a
    identidade do contrato, e mudá-los depois de gerar parcelas produz um
    registro que não corresponde a nenhum documento assinado.
    """

    description: str | None = Field(default=None, max_length=500)
    monthly_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    end_date: date | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    auto_renew: bool | None = None
    renewal_term_months: int | None = Field(default=None, ge=1, le=120)
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Versão lida pelo cliente. Se enviada e divergente da atual, a alteração "
            "é recusada com 409 — evita sobrescrever a edição de outro operador."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateContractRequest:
        campos = (
            self.description,
            self.monthly_amount,
            self.end_date,
            self.due_day,
            self.auto_renew,
            self.renewal_term_months,
        )
        if all(value is None for value in campos):
            raise ValueError("informe ao menos um campo para alterar")
        return self


class CancelContractRequest(StrictModel):
    reason: str = Field(min_length=3, max_length=300)


class RenewContractRequest(StrictModel):
    term_months: int | None = Field(default=None, ge=1, le=120)
    adjustment_percent: Decimal | None = Field(
        default=None,
        ge=Decimal("-50"),
        le=Decimal("100"),
        description="Reajuste aplicado à mensalidade, em porcentagem",
    )


class ContractResponse(OutputModel):
    id: uuid.UUID
    client_id: uuid.UUID
    number: str
    description: str | None
    monthly_amount: Decimal
    start_date: date
    end_date: date
    due_day: int
    status: ContractStatus
    auto_renew: bool
    renewal_term_months: int
    version: int
    cancelled_at: datetime | None
    cancellation_reason: str | None
    renewed_to_id: uuid.UUID | None
    previous_contract_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @field_validator("monthly_amount", mode="before")
    @classmethod
    def _unwrap_money(cls, value: object) -> Decimal:
        return value.amount if isinstance(value, Money) else Decimal(str(value))


class InstallmentResponse(OutputModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    competence: str
    due_date: date
    amount: Decimal
    status: InstallmentStatus
    paid_at: datetime | None

    @field_validator("amount", mode="before")
    @classmethod
    def _unwrap_money(cls, value: object) -> Decimal:
        return value.amount if isinstance(value, Money) else Decimal(str(value))


class ContractEventResponse(OutputModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    event_type: str
    payload: dict
    actor_user_id: uuid.UUID | None
    occurred_at: datetime


class NotificationResponse(OutputModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    notification_type: NotificationType
    channel: str
    recipient: str
    subject: str
    status: NotificationStatus
    attempts: int
    last_error: str | None
    provider_message_id: str | None
    sent_at: datetime | None
    created_at: datetime

    @field_validator("recipient", mode="before")
    @classmethod
    def _mask_recipient(cls, value: object) -> str:
        """Mascara o destinatário na listagem.

        Quem consulta o painel de notificações precisa saber se o aviso saiu,
        não colher a lista de e-mails dos clientes. O endereço completo continua
        no cadastro do cliente, atrás de `client:read`.
        """
        text = str(value)
        if "@" not in text:
            return f"{text[:3]}***"
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"


class JobRunResponse(OutputModel):
    id: uuid.UUID
    job_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    stats: dict
    error_message: str | None
