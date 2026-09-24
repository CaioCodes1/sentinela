"""Unidade de Trabalho.

Um caso de uso é uma transação. "Criar contrato" grava o contrato, gera as
parcelas, escreve o evento de histórico e a linha de auditoria — e ou tudo isso
acontece, ou nada acontece. Sem esse limite explícito, cada repositório comita
por conta própria e uma falha no meio deixa contrato sem parcela e auditoria
mentindo que deu certo.

O `with` também garante o `rollback` no caminho de exceção, que é o passo que
se esquece quando o commit fica espalhado.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from app.domain.ports.repositories import (
    AuditRepository,
    ClientRepository,
    ContractEventRepository,
    ContractRepository,
    InstallmentRepository,
    JobRunRepository,
    NotificationRepository,
    RefreshTokenRepository,
    UserRepository,
)


class UnitOfWork(Protocol):
    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    clients: ClientRepository
    contracts: ContractRepository
    installments: InstallmentRepository
    contract_events: ContractEventRepository
    notifications: NotificationRepository
    audit: AuditRepository
    job_runs: JobRunRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def flush(self) -> None:
        """Empurra o SQL sem fechar a transação.

        Necessário quando o passo seguinte depende de uma restrição do banco —
        por exemplo, descobrir que o número do contrato já existe **antes** de
        gerar as parcelas.
        """
        ...

    def try_advisory_lock(self, key: str) -> bool:
        """Trava de exclusão mútua entre processos, no próprio PostgreSQL.

        É o que impede duas réplicas da API de rodarem o job das 3h ao mesmo
        tempo e enviarem tudo em duplicidade. Uma variável de módulo não
        resolveria: cada réplica tem a sua.
        """
        ...
