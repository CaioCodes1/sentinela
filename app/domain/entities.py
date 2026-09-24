"""Entidades do domínio.

Estas classes **não importam SQLAlchemy**. O vínculo com as tabelas é feito de
fora, por mapeamento imperativo, em `app/infrastructure/db/mappers.py`.

Por que assim, e não com `DeclarativeBase` como quase todo tutorial mostra: o
mapeamento declarativo obriga a entidade a herdar de uma classe do ORM, e a
partir daí a regra de negócio só roda se o SQLAlchemy estiver configurado. Um
teste de "contrato cancelado não pode ser renovado" passa a precisar de banco.
Aqui ele é `Contract(...)` e uma chamada de método — milissegundos, sem Docker.

O preço dessa escolha está registrado no ADR-002.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from app.domain.enums import (
    AuditAction,
    AuditOutcome,
    ClientStatus,
    ContractStatus,
    InstallmentStatus,
    JobStatus,
    NotificationStatus,
    NotificationType,
    Role,
)
from app.domain.exceptions import BusinessRuleError, ValidationError
from app.domain.rules import (
    ensure_can_be_modified,
    validate_contract_period,
    validate_monthly_amount,
)
from app.domain.value_objects import EmailAddress, Money, TaxId, normalize_text


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> uuid.UUID:
    """UUIDv4 gerado na aplicação.

    Id sequencial exposto em URL entrega duas informações de graça: quantos
    registros existem e quais são os vizinhos. `/contracts/1` convida a tentar
    `/contracts/2` — enumeração é a porta de entrada de IDOR.
    """
    return uuid.uuid4()


class User:
    """Usuário do sistema. A senha só existe aqui como hash."""

    def __init__(
        self,
        *,
        email: EmailAddress,
        password_hash: str,
        role: Role,
        full_name: str,
        id: uuid.UUID | None = None,
        is_active: bool = True,
    ) -> None:
        self.id = id or _new_id()
        self.email = email
        self.password_hash = password_hash
        self.role = role
        self.full_name = normalize_text(full_name, max_length=120, field="nome")
        self.is_active = is_active
        self.failed_login_attempts = 0
        self.locked_until: datetime | None = None
        self.last_login_at: datetime | None = None
        self.password_changed_at = _now()
        self.created_at = _now()
        self.updated_at = _now()

    def is_locked(self, now: datetime | None = None) -> bool:
        if self.locked_until is None:
            return False
        return self.locked_until > (now or _now())

    def register_failed_login(
        self, *, max_attempts: int, lockout_minutes: int, now: datetime | None = None
    ) -> bool:
        """Conta a tentativa e bloqueia ao atingir o teto. Devolve se bloqueou agora."""
        from datetime import timedelta

        current = now or _now()
        self.failed_login_attempts += 1
        self.updated_at = current
        if self.failed_login_attempts >= max_attempts:
            self.locked_until = current + timedelta(minutes=lockout_minutes)
            return True
        return False

    def register_successful_login(self, now: datetime | None = None) -> None:
        current = now or _now()
        self.failed_login_attempts = 0
        self.locked_until = None
        self.last_login_at = current
        self.updated_at = current

    def change_password(self, new_hash: str) -> None:
        self.password_hash = new_hash
        self.password_changed_at = _now()
        self.updated_at = _now()


class RefreshToken:
    """Refresh token persistido como hash, em família rotativa.

    Guardar o token em claro no banco significa que um dump vira sessão válida
    para todos os usuários. Guardamos SHA-256: serve para localizar e comparar,
    não para reemitir.
    """

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        family_id: uuid.UUID,
        expires_at: datetime,
        id: uuid.UUID | None = None,
        created_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.user_id = user_id
        self.token_hash = token_hash
        self.family_id = family_id
        self.expires_at = expires_at
        self.created_ip = created_ip
        self.user_agent = user_agent
        self.revoked_at: datetime | None = None
        self.replaced_by_id: uuid.UUID | None = None
        self.created_at = _now()

    def is_usable(self, now: datetime | None = None) -> bool:
        current = now or _now()
        return self.revoked_at is None and self.expires_at > current

    def revoke(
        self, *, replaced_by_id: uuid.UUID | None = None, now: datetime | None = None
    ) -> None:
        if self.revoked_at is None:
            self.revoked_at = now or _now()
        self.replaced_by_id = replaced_by_id


class Client:
    def __init__(
        self,
        *,
        tax_id: TaxId,
        legal_name: str,
        email: EmailAddress,
        phone: str | None = None,
        trade_name: str | None = None,
        id: uuid.UUID | None = None,
        created_by_id: uuid.UUID | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.tax_id = tax_id
        self.legal_name = normalize_text(legal_name, max_length=180, field="razão social")
        self.trade_name = (
            normalize_text(trade_name, max_length=180, field="nome fantasia")
            if trade_name
            else None
        )
        self.email = email
        self.phone = _normalize_phone(phone) if phone else None
        self.status = ClientStatus.ACTIVE
        self.created_by_id = created_by_id
        self.created_at = _now()
        self.updated_at = _now()

    @property
    def is_active(self) -> bool:
        return self.status is ClientStatus.ACTIVE

    def update(
        self,
        *,
        legal_name: str | None = None,
        trade_name: str | None = None,
        email: EmailAddress | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """Aplica a alteração e devolve o diff — é o diff que vai para a auditoria.

        Registrar "cliente alterado" sem dizer o quê torna a trilha inútil no dia
        em que alguém precisa saber quem trocou o e-mail de cobrança.
        """
        changes: dict[str, Any] = {}
        if legal_name is not None:
            new = normalize_text(legal_name, max_length=180, field="razão social")
            if new != self.legal_name:
                changes["legal_name"] = {"de": self.legal_name, "para": new}
                self.legal_name = new
        if trade_name is not None:
            new_trade = normalize_text(trade_name, max_length=180, field="nome fantasia")
            if new_trade != self.trade_name:
                changes["trade_name"] = {"de": self.trade_name, "para": new_trade}
                self.trade_name = new_trade
        if email is not None and email.value != self.email.value:
            # O valor cru do e-mail não entra no diff: a auditoria é lida por
            # perfis que não têm acesso ao cadastro completo.
            changes["email"] = {"de": self.email.masked(), "para": email.masked()}
            self.email = email
        if phone is not None:
            new_phone = _normalize_phone(phone)
            if new_phone != self.phone:
                changes["phone"] = {"alterado": True}
                self.phone = new_phone
        if changes:
            self.updated_at = _now()
        return changes

    def deactivate(self) -> None:
        if self.status is ClientStatus.INACTIVE:
            raise BusinessRuleError("cliente já está inativo")
        self.status = ClientStatus.INACTIVE
        self.updated_at = _now()


def _normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not 10 <= len(digits) <= 13:
        raise ValidationError("telefone precisa ter entre 10 e 13 dígitos")
    return digits


class Contract:
    def __init__(
        self,
        *,
        client_id: uuid.UUID,
        number: str,
        monthly_amount: Money,
        start_date: date,
        end_date: date,
        due_day: int,
        description: str | None = None,
        auto_renew: bool = False,
        renewal_term_months: int = 12,
        id: uuid.UUID | None = None,
        created_by_id: uuid.UUID | None = None,
    ) -> None:
        validate_contract_period(start_date, end_date)
        validate_monthly_amount(monthly_amount)
        if not 1 <= due_day <= 31:
            raise ValidationError("dia de vencimento precisa estar entre 1 e 31")

        self.id = id or _new_id()
        self.client_id = client_id
        self.number = normalize_text(number, max_length=40, field="número do contrato").upper()
        self.description = (
            normalize_text(description, max_length=500, field="descrição") if description else None
        )
        self.monthly_amount = monthly_amount
        self.start_date = start_date
        self.end_date = end_date
        self.due_day = due_day
        self.status = ContractStatus.ACTIVE
        self.auto_renew = auto_renew
        self.renewal_term_months = renewal_term_months
        self.cancelled_at: datetime | None = None
        self.cancellation_reason: str | None = None
        self.renewed_to_id: uuid.UUID | None = None
        self.previous_contract_id: uuid.UUID | None = None
        # Trava otimista. Começa em 1 e sobe a cada escrita; quem tentar gravar
        # com uma versão velha recebe conflito em vez de sobrescrever o outro.
        self.version = 1
        self.created_by_id = created_by_id
        self.created_at = _now()
        self.updated_at = _now()

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = _now()

    def update(
        self,
        *,
        description: str | None = None,
        monthly_amount: Money | None = None,
        end_date: date | None = None,
        due_day: int | None = None,
        auto_renew: bool | None = None,
        renewal_term_months: int | None = None,
    ) -> dict[str, Any]:
        ensure_can_be_modified(self.status, "editar")
        changes: dict[str, Any] = {}

        if description is not None:
            new = normalize_text(description, max_length=500, field="descrição")
            if new != self.description:
                changes["description"] = {"de": self.description, "para": new}
                self.description = new
        if monthly_amount is not None and monthly_amount != self.monthly_amount:
            validate_monthly_amount(monthly_amount)
            changes["monthly_amount"] = {
                "de": str(self.monthly_amount),
                "para": str(monthly_amount),
            }
            self.monthly_amount = monthly_amount
        if end_date is not None and end_date != self.end_date:
            validate_contract_period(self.start_date, end_date)
            changes["end_date"] = {"de": self.end_date.isoformat(), "para": end_date.isoformat()}
            self.end_date = end_date
        if due_day is not None and due_day != self.due_day:
            if not 1 <= due_day <= 31:
                raise ValidationError("dia de vencimento precisa estar entre 1 e 31")
            changes["due_day"] = {"de": self.due_day, "para": due_day}
            self.due_day = due_day
        if auto_renew is not None and auto_renew != self.auto_renew:
            changes["auto_renew"] = {"de": self.auto_renew, "para": auto_renew}
            self.auto_renew = auto_renew
        if renewal_term_months is not None and renewal_term_months != self.renewal_term_months:
            changes["renewal_term_months"] = {
                "de": self.renewal_term_months,
                "para": renewal_term_months,
            }
            self.renewal_term_months = renewal_term_months

        if changes:
            self._touch()
        return changes

    def cancel(self, reason: str, *, now: datetime | None = None) -> None:
        ensure_can_be_modified(self.status, "cancelar")
        self.cancellation_reason = normalize_text(reason, max_length=300, field="motivo")
        self.cancelled_at = now or _now()
        self.status = ContractStatus.CANCELLED
        self._touch()

    def mark_expiring(self) -> None:
        if self.status is ContractStatus.ACTIVE:
            self.status = ContractStatus.EXPIRING
            self._touch()

    def mark_expired(self) -> None:
        if self.status in (ContractStatus.ACTIVE, ContractStatus.EXPIRING):
            self.status = ContractStatus.EXPIRED
            self._touch()

    def mark_renewed(self, successor_id: uuid.UUID) -> None:
        self.status = ContractStatus.RENEWED
        self.renewed_to_id = successor_id
        self._touch()

    @property
    def is_open(self) -> bool:
        return self.status in (ContractStatus.ACTIVE, ContractStatus.EXPIRING)


class Installment:
    """Mensalidade de um contrato, uma por competência."""

    def __init__(
        self,
        *,
        contract_id: uuid.UUID,
        competence: str,
        due_date: date,
        amount: Money,
        id: uuid.UUID | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.contract_id = contract_id
        self.competence = competence
        self.due_date = due_date
        self.amount = amount
        self.status = InstallmentStatus.PENDING
        self.paid_at: datetime | None = None
        self.created_at = _now()
        self.updated_at = _now()

    def mark_paid(self, *, now: datetime | None = None) -> None:
        if self.status is InstallmentStatus.PAID:
            raise BusinessRuleError("parcela já está quitada")
        if self.status is InstallmentStatus.CANCELLED:
            raise BusinessRuleError("parcela cancelada não pode ser quitada")
        self.status = InstallmentStatus.PAID
        self.paid_at = now or _now()
        self.updated_at = self.paid_at

    def mark_overdue(self) -> None:
        if self.status is InstallmentStatus.PENDING:
            self.status = InstallmentStatus.OVERDUE
            self.updated_at = _now()


class ContractEvent:
    """Linha do histórico de um contrato. Escrita uma vez, nunca alterada."""

    def __init__(
        self,
        *,
        contract_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        actor_user_id: uuid.UUID | None = None,
        id: uuid.UUID | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.contract_id = contract_id
        self.event_type = event_type
        self.payload = payload
        self.actor_user_id = actor_user_id
        self.occurred_at = _now()


class Notification:
    def __init__(
        self,
        *,
        contract_id: uuid.UUID,
        notification_type: NotificationType,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        dedupe_key: str,
        id: uuid.UUID | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.contract_id = contract_id
        self.notification_type = notification_type
        self.channel = channel
        self.recipient = recipient
        self.subject = subject
        self.body = body
        self.dedupe_key = dedupe_key
        self.status = NotificationStatus.PENDING
        self.attempts = 0
        self.last_error: str | None = None
        self.provider_message_id: str | None = None
        self.sent_at: datetime | None = None
        self.created_at = _now()
        self.updated_at = _now()

    def register_success(self, provider_message_id: str | None) -> None:
        self.attempts += 1
        self.status = NotificationStatus.SENT
        self.provider_message_id = provider_message_id
        self.sent_at = _now()
        self.last_error = None
        self.updated_at = self.sent_at

    def register_failure(self, error: str, *, max_attempts: int) -> None:
        self.attempts += 1
        # A mensagem do provedor é truncada e nunca interpretada: ela vem de
        # fora e já apareceu carregando conteúdo que não deveria ser ecoado.
        self.last_error = error[:500]
        self.status = (
            NotificationStatus.DEAD_LETTER
            if self.attempts >= max_attempts
            else NotificationStatus.FAILED
        )
        self.updated_at = _now()

    @property
    def can_retry(self) -> bool:
        return self.status in (NotificationStatus.PENDING, NotificationStatus.FAILED)


class AuditLog:
    """Registro de auditoria. Imutável — o banco recusa UPDATE e DELETE nesta tabela."""

    def __init__(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome,
        resource_type: str,
        resource_id: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        actor_role: Role | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
        id: uuid.UUID | None = None,
    ) -> None:
        self.id = id or _new_id()
        self.action = action
        self.outcome = outcome
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.actor_user_id = actor_user_id
        # Cópia do e-mail e do papel no momento do ato. Se o usuário for
        # renomeado ou promovido depois, a trilha continua dizendo quem era
        # aquela pessoa naquele dia — que é o ponto de auditoria.
        self.actor_email = actor_email
        self.actor_role = actor_role
        self.ip_address = ip_address
        self.user_agent = (user_agent or "")[:300] or None
        self.request_id = request_id
        self.details = details or {}
        self.occurred_at = _now()


class JobRun:
    """Execução da automação diária. Sem isto, "o job rodou?" não tem resposta."""

    def __init__(self, *, job_name: str, id: uuid.UUID | None = None) -> None:
        self.id = id or _new_id()
        self.job_name = job_name
        self.status = JobStatus.RUNNING
        self.started_at = _now()
        self.finished_at: datetime | None = None
        self.stats: dict[str, Any] = {}
        self.error_message: str | None = None

    def finish(self, stats: dict[str, Any]) -> None:
        self.status = JobStatus.SUCCESS
        self.stats = stats
        self.finished_at = _now()

    def fail(self, error: str, stats: dict[str, Any] | None = None) -> None:
        self.status = JobStatus.FAILED
        self.error_message = error[:1000]
        self.stats = stats or {}
        self.finished_at = _now()

    def skip(self, reason: str) -> None:
        self.status = JobStatus.SKIPPED
        self.error_message = reason
        self.finished_at = _now()

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()
