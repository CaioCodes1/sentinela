"""Portas de persistência (Repository Pattern).

Estas interfaces pertencem ao **domínio**, não à infraestrutura. É a inversão de
dependência do "D" de SOLID: quem define o contrato é quem consome
(`app/services/`), e o SQLAlchemy em `app/infrastructure/` é que se adapta.

Consequência prática, e o motivo de valer o trabalho: o serviço de contratos é
testável com um repositório de dicionário em memória, sem banco, sem Docker e
sem migration. Os testes unitários em `tests/unit/` fazem exatamente isso.

São `Protocol` e não classes abstratas: tipagem estrutural dispensa herança, e
o duplo em memória do teste não precisa importar nada do domínio para ser
aceito pelo mypy.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Protocol, runtime_checkable

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
from app.domain.enums import ContractStatus, NotificationStatus


@runtime_checkable
class UserRepository(Protocol):
    def add(self, user: User) -> None: ...

    def get(self, user_id: uuid.UUID) -> User | None: ...

    def get_by_email(self, email: str) -> User | None: ...

    def list_all(self) -> list[User]: ...


@runtime_checkable
class RefreshTokenRepository(Protocol):
    def add(self, token: RefreshToken) -> None: ...

    def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...

    def list_by_family(self, family_id: uuid.UUID) -> list[RefreshToken]: ...

    def revoke_family(self, family_id: uuid.UUID, *, now: datetime) -> int: ...

    def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int: ...

    def purge_expired(self, *, before: datetime) -> int: ...


@runtime_checkable
class ClientRepository(Protocol):
    def add(self, client: Client) -> None: ...

    def get(self, client_id: uuid.UUID) -> Client | None: ...

    def get_by_tax_id(self, tax_id_digits: str) -> Client | None: ...

    def search(
        self,
        *,
        term: str | None = None,
        only_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Client], int]:
        """Devolve `(página, total)`.

        O total vem junto porque paginação sem total força o cliente da API a
        adivinhar se existe próxima página.
        """
        ...


@runtime_checkable
class ContractRepository(Protocol):
    def add(self, contract: Contract) -> None: ...

    def get(self, contract_id: uuid.UUID) -> Contract | None: ...

    def get_by_number(self, number: str) -> Contract | None: ...

    def search(
        self,
        *,
        client_id: uuid.UUID | None = None,
        status: ContractStatus | None = None,
        expiring_until: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Contract], int]: ...

    def list_open_contracts(self) -> list[Contract]:
        """Contratos em ACTIVE ou EXPIRING — a matéria-prima do job diário."""
        ...

    def count_open_by_client(self, client_id: uuid.UUID) -> int: ...


@runtime_checkable
class InstallmentRepository(Protocol):
    def add(self, installment: Installment) -> None: ...

    def add_many_ignoring_duplicates(self, installments: list[Installment]) -> int:
        """Insere em lote ignorando colisão de (contrato, competência).

        Existe como método próprio porque a alternativa — `try/except` em volta
        de cada insert — **não funciona no PostgreSQL**: o primeiro erro aborta
        a transação inteira e todo comando seguinte é recusado com
        `current transaction is aborted`. A deduplicação tem que acontecer
        dentro do comando, com `ON CONFLICT DO NOTHING`.
        """
        ...

    def list_by_contract(self, contract_id: uuid.UUID) -> list[Installment]: ...

    def list_overdue(self, *, reference: date) -> list[Installment]: ...

    def get(self, installment_id: uuid.UUID) -> Installment | None: ...


@runtime_checkable
class ContractEventRepository(Protocol):
    def add(self, event: ContractEvent) -> None: ...

    def list_by_contract(
        self, contract_id: uuid.UUID, *, limit: int = 100
    ) -> list[ContractEvent]: ...


@runtime_checkable
class NotificationRepository(Protocol):
    def add_if_absent(self, notification: Notification) -> bool:
        """Insere só se a `dedupe_key` ainda não existir. Devolve se inseriu."""
        ...

    def get(self, notification_id: uuid.UUID) -> Notification | None: ...

    def list_dispatchable(self, *, limit: int = 200) -> list[Notification]: ...

    def search(
        self,
        *,
        contract_id: uuid.UUID | None = None,
        status: NotificationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Notification], int]: ...


@runtime_checkable
class AuditRepository(Protocol):
    def add(self, entry: AuditLog) -> None: ...

    def search(
        self,
        *,
        actor_user_id: uuid.UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]: ...


@runtime_checkable
class JobRunRepository(Protocol):
    def add(self, run: JobRun) -> None: ...

    def list_recent(self, *, job_name: str | None = None, limit: int = 20) -> list[JobRun]: ...
