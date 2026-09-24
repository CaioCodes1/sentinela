"""Dublês em memória dos repositórios e da unidade de trabalho.

Estes dublês são a razão prática de as portas existirem em `app/domain/ports/`.
Com eles, `tests/unit/` exercita regra de negócio de verdade — inclusive
transição de estado e geração de parcela — em milissegundos, sem PostgreSQL,
sem Docker e sem migration.

O que eles **não** provam: nada que dependa do banco. Índice único, `ON CONFLICT`,
gatilho de imutabilidade, trava otimista e advisory lock só são verificáveis em
`tests/integration/`. Confundir as duas coisas é como se cria a suíte que fica
verde enquanto a produção quebra — por isso o dublê de notificações aqui imita
a deduplicação, mas o teste que **prova** a idempotência é o de integração.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from types import TracebackType
from typing import Any

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
from app.domain.enums import ContractStatus, InstallmentStatus, NotificationStatus
from app.domain.ports.notifier import NotificationRequest, NotificationResult


class FakeUserRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, User] = {}

    def add(self, user: User) -> None:
        self.items[user.id] = user

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.items.get(user_id)

    def get_by_email(self, email: str) -> User | None:
        alvo = email.lower()
        return next((u for u in self.items.values() if u.email.value.lower() == alvo), None)

    def list_all(self) -> list[User]:
        return list(self.items.values())


class FakeRefreshTokenRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, RefreshToken] = {}

    def add(self, token: RefreshToken) -> None:
        self.items[token.id] = token

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return next((t for t in self.items.values() if t.token_hash == token_hash), None)

    def list_by_family(self, family_id: uuid.UUID) -> list[RefreshToken]:
        return [t for t in self.items.values() if t.family_id == family_id]

    def revoke_family(self, family_id: uuid.UUID, *, now: datetime) -> int:
        alvos = [
            t for t in self.items.values() if t.family_id == family_id and t.revoked_at is None
        ]
        for token in alvos:
            token.revoke(now=now)
        return len(alvos)

    def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        alvos = [t for t in self.items.values() if t.user_id == user_id and t.revoked_at is None]
        for token in alvos:
            token.revoke(now=now)
        return len(alvos)

    def purge_expired(self, *, before: datetime) -> int:
        alvos = [k for k, v in self.items.items() if v.expires_at < before]
        for key in alvos:
            del self.items[key]
        return len(alvos)


class FakeClientRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Client] = {}

    def add(self, client: Client) -> None:
        self.items[client.id] = client

    def get(self, client_id: uuid.UUID) -> Client | None:
        return self.items.get(client_id)

    def get_by_tax_id(self, tax_id_digits: str) -> Client | None:
        return next((c for c in self.items.values() if c.tax_id.digits == tax_id_digits), None)

    def search(
        self,
        *,
        term: str | None = None,
        only_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Client], int]:
        resultado = list(self.items.values())
        if term:
            alvo = term.lower()
            resultado = [
                c for c in resultado if alvo in c.legal_name.lower() or alvo in c.tax_id.digits
            ]
        if only_active is not None:
            resultado = [c for c in resultado if c.is_active is only_active]
        return resultado[offset : offset + limit], len(resultado)


class FakeContractRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Contract] = {}

    def add(self, contract: Contract) -> None:
        self.items[contract.id] = contract

    def get(self, contract_id: uuid.UUID) -> Contract | None:
        return self.items.get(contract_id)

    def get_by_number(self, number: str) -> Contract | None:
        return next((c for c in self.items.values() if c.number == number.upper()), None)

    def search(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: ContractStatus | None = None,
        expiring_until: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Contract], int]:
        resultado = list(self.items.values())
        if client_id is not None:
            resultado = [c for c in resultado if c.client_id == client_id]
        if status is not None:
            resultado = [c for c in resultado if c.status is status]
        if expiring_until is not None:
            resultado = [c for c in resultado if c.end_date <= expiring_until and c.is_open]
        return resultado[offset : offset + limit], len(resultado)

    def list_open_contracts(self) -> list[Contract]:
        return [c for c in self.items.values() if c.is_open]

    def count_open_by_client(self, client_id: uuid.UUID) -> int:
        return sum(1 for c in self.items.values() if c.client_id == client_id and c.is_open)


class FakeInstallmentRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Installment] = {}

    def add(self, installment: Installment) -> None:
        self.items[installment.id] = installment

    def add_many_ignoring_duplicates(self, installments: list[Installment]) -> int:
        existentes = {(i.contract_id, i.competence) for i in self.items.values()}
        inseridos = 0
        for item in installments:
            chave = (item.contract_id, item.competence)
            if chave in existentes:
                continue
            self.items[item.id] = item
            existentes.add(chave)
            inseridos += 1
        return inseridos

    def get(self, installment_id: uuid.UUID) -> Installment | None:
        return self.items.get(installment_id)

    def list_by_contract(self, contract_id: uuid.UUID) -> list[Installment]:
        return sorted(
            (i for i in self.items.values() if i.contract_id == contract_id),
            key=lambda i: i.due_date,
        )

    def list_overdue(self, *, reference: date) -> list[Installment]:
        return [
            i
            for i in self.items.values()
            if i.due_date < reference
            and i.status in (InstallmentStatus.PENDING, InstallmentStatus.OVERDUE)
        ]


class FakeContractEventRepository:
    def __init__(self) -> None:
        self.items: list[ContractEvent] = []

    def add(self, event: ContractEvent) -> None:
        self.items.append(event)

    def list_by_contract(self, contract_id: uuid.UUID, *, limit: int = 100) -> list[ContractEvent]:
        return [e for e in self.items if e.contract_id == contract_id][:limit]


class FakeNotificationRepository:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Notification] = {}
        self._chaves: set[str] = set()

    def add_if_absent(self, notification: Notification) -> bool:
        if notification.dedupe_key in self._chaves:
            return False
        self._chaves.add(notification.dedupe_key)
        self.items[notification.id] = notification
        return True

    def get(self, notification_id: uuid.UUID) -> Notification | None:
        return self.items.get(notification_id)

    def list_dispatchable(self, *, limit: int = 200) -> list[Notification]:
        return sorted(
            (
                n
                for n in self.items.values()
                if n.status in (NotificationStatus.PENDING, NotificationStatus.FAILED)
            ),
            key=lambda n: n.created_at,
        )[:limit]

    def search(
        self,
        *,
        contract_id: uuid.UUID | None = None,
        status: NotificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Notification], int]:
        resultado = list(self.items.values())
        if contract_id is not None:
            resultado = [n for n in resultado if n.contract_id == contract_id]
        if status is not None:
            resultado = [n for n in resultado if n.status is status]
        return resultado[offset : offset + limit], len(resultado)


class FakeAuditRepository:
    def __init__(self) -> None:
        self.items: list[AuditLog] = []

    def add(self, entry: AuditLog) -> None:
        self.items.append(entry)

    def search(self, **kwargs: Any) -> tuple[list[AuditLog], int]:
        return self.items, len(self.items)

    def actions(self) -> list[str]:
        """Atalho para asserção: `assert "CONTRACT_RENEWED" in uow.audit.actions()`."""
        return [entry.action.value for entry in self.items]


class FakeJobRunRepository:
    def __init__(self) -> None:
        self.items: list[JobRun] = []

    def add(self, run: JobRun) -> None:
        self.items.append(run)

    def list_recent(self, *, job_name: str | None = None, limit: int = 20) -> list[JobRun]:
        resultado = [r for r in self.items if job_name is None or r.job_name == job_name]
        return resultado[-limit:]


class FakeUnitOfWork:
    """Unidade de trabalho em memória.

    `commit()` apenas conta chamadas — não há transação para confirmar. Isso é
    suficiente para asserções do tipo "o caso de uso comitou uma vez só", que é
    o que se quer verificar no nível unitário.
    """

    def __init__(self, *, lock_disponivel: bool = True) -> None:
        self.users = FakeUserRepository()
        self.refresh_tokens = FakeRefreshTokenRepository()
        self.clients = FakeClientRepository()
        self.contracts = FakeContractRepository()
        self.installments = FakeInstallmentRepository()
        self.contract_events = FakeContractEventRepository()
        self.notifications = FakeNotificationRepository()
        self.audit = FakeAuditRepository()
        self.job_runs = FakeJobRunRepository()
        self.commits = 0
        self.rollbacks = 0
        self._lock_disponivel = lock_disponivel

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def flush(self) -> None:
        return None

    def try_advisory_lock(self, key: str) -> bool:
        return self._lock_disponivel


class FakeNotificationGateway:
    """Provedor de notificação controlável.

    `respostas` é uma fila: cada `send` consome a próxima. Esgotada, devolve
    sucesso. Permite roteirizar "falha, falha, sucesso" e verificar que a
    notificação só é dada por perdida depois do número certo de tentativas.
    """

    def __init__(self, *, respostas: list[NotificationResult] | None = None) -> None:
        self.enviadas: list[NotificationRequest] = []
        self.respostas = respostas or []
        self.saudavel = True

    def send(self, request: NotificationRequest) -> NotificationResult:
        self.enviadas.append(request)
        if self.respostas:
            return self.respostas.pop(0)
        return NotificationResult(delivered=True, provider_message_id="msg_fake", attempts=1)

    def health(self) -> bool:
        return self.saudavel
