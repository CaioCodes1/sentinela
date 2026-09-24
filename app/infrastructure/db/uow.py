"""Unidade de trabalho sobre uma `Session` do SQLAlchemy."""

from __future__ import annotations

import hashlib
from types import TracebackType

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.core.logging import get_logger
from app.domain.exceptions import ConcurrencyError
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
from app.infrastructure.repositories.audit_repository import SqlAuditRepository
from app.infrastructure.repositories.client_repository import SqlClientRepository
from app.infrastructure.repositories.contract_event_repository import (
    SqlContractEventRepository,
)
from app.infrastructure.repositories.contract_repository import SqlContractRepository
from app.infrastructure.repositories.installment_repository import SqlInstallmentRepository
from app.infrastructure.repositories.job_run_repository import SqlJobRunRepository
from app.infrastructure.repositories.notification_repository import SqlNotificationRepository
from app.infrastructure.repositories.refresh_token_repository import (
    SqlRefreshTokenRepository,
)
from app.infrastructure.repositories.user_repository import SqlUserRepository

logger = get_logger(__name__)


class SqlAlchemyUnitOfWork:
    """Uma transação, um conjunto de repositórios compartilhando a mesma sessão.

    A sessão compartilhada não é detalhe: é o que faz duas escritas na mesma
    unidade enxergarem uma à outra antes do commit, e o que permite reverter
    tudo junto.
    """

    # Os atributos são anotados com o tipo da **porta**, não da implementação.
    #
    # Isso não é cosmético: sem a anotação explícita, o mypy não reconhece que
    # esta classe satisfaz o `Protocol` `UnitOfWork` — atributos atribuídos só
    # dentro de `__enter__` não entram na interface da classe, e todo serviço
    # que recebe um `UnitOfWork` acusa incompatibilidade.
    #
    # Ou seja: sem estas linhas, a inversão de dependência estava escrita mas
    # **não verificada**. Com elas, o mypy passa a provar que o adaptador
    # concreto cumpre o contrato que o domínio declarou.
    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    clients: ClientRepository
    contracts: ContractRepository
    installments: InstallmentRepository
    contract_events: ContractEventRepository
    notifications: NotificationRepository
    audit: AuditRepository
    job_runs: JobRunRepository

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session = self._session_factory()
        self.users = SqlUserRepository(self.session)
        self.refresh_tokens = SqlRefreshTokenRepository(self.session)
        self.clients = SqlClientRepository(self.session)
        self.contracts = SqlContractRepository(self.session)
        self.installments = SqlInstallmentRepository(self.session)
        self.contract_events = SqlContractEventRepository(self.session)
        self.notifications = SqlNotificationRepository(self.session)
        self.audit = SqlAuditRepository(self.session)
        self.job_runs = SqlJobRunRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self.session is not None
        try:
            # Rollback incondicional na saída. Se `commit()` já rodou, este
            # rollback não tem efeito; se ninguém comitou, ele desfaz — e é
            # essa a rede de segurança que impede uma escrita parcial de ficar
            # pendurada na conexão devolvida ao pool.
            self.session.rollback()
        finally:
            self.session.close()
            self.session = None

    def commit(self) -> None:
        assert self.session is not None
        try:
            self.session.commit()
        except StaleDataError as exc:
            # A trava otimista disparou: alguém gravou este contrato entre a
            # leitura e a escrita. Traduzido para erro de domínio aqui, para
            # que a camada de serviço não conheça exceções do ORM.
            self.session.rollback()
            raise ConcurrencyError(
                "o registro foi alterado por outra operação; recarregue e tente de novo"
            ) from exc

    def rollback(self) -> None:
        assert self.session is not None
        self.session.rollback()

    def flush(self) -> None:
        assert self.session is not None
        self.session.flush()

    def try_advisory_lock(self, key: str) -> bool:
        """`pg_try_advisory_xact_lock`: pega ou desiste, nunca espera.

        A variante *xact* é liberada automaticamente no fim da transação —
        inclusive se o processo morrer. A versão de sessão (`pg_advisory_lock`)
        precisaria de unlock explícito, e um `kill -9` no meio do job deixaria
        a trava presa até alguém reiniciar o banco: a automação simplesmente
        pararia de rodar, em silêncio, para sempre.

        Não espera porque a semântica desejada é "se outra réplica já está
        fazendo isso, não faça de novo" — e não "faça de novo daqui a pouco".
        """
        assert self.session is not None
        # O PostgreSQL só aceita chave numérica de 64 bits; o hash converte um
        # nome legível em número estável entre processos e reinicializações.
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        lock_id = int.from_bytes(digest[:8], "big", signed=True)
        result = self.session.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id}
        ).scalar_one()
        if not result:
            logger.info("trava de exclusão já tomada", extra={"lock_key": key})
        return bool(result)
