"""Criação do engine e da fábrica de sessões.

O engine é caro (abre pool de conexões) e precisa existir **uma vez por
processo**. Criá-lo por requisição é o erro que faz a aplicação esgotar as
conexões do PostgreSQL sob carga, com sintoma de "banco lento" e causa no
código da aplicação.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.infrastructure.db.mappers import configure_mappers

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # Sonda a conexão antes de entregá-la. Sem isto, toda vez que o banco
        # reinicia (ou um firewall derruba conexão ociosa), a primeira
        # requisição de cada conexão do pool falha com "server closed the
        # connection unexpectedly" — erro que aparece só em produção.
        pool_pre_ping=settings.db_pool_pre_ping,
        # Recicla antes do tempo de ociosidade típico de balanceador (1h).
        pool_recycle=1800,
        echo=settings.db_echo,
        future=True,
        connect_args={
            "application_name": f"sentinela-{settings.app_env}",
            # Nenhuma consulta desta aplicação deveria levar 15 segundos. O
            # teto transforma uma consulta patológica em erro localizado, em
            # vez de uma conexão presa segurando o pool inteiro.
            "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=30000",
        },
    )

    @event.listens_for(engine, "connect")
    def _harden_session(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        """Endurece cada conexão nova, do lado do banco."""
        with dbapi_connection.cursor() as cursor:
            # Timestamps sempre em UTC, independentemente do fuso do servidor.
            cursor.execute("SET TIME ZONE 'UTC'")
            # `search_path` explícito: sem ele, um schema malicioso no caminho
            # de busca poderia sombrear uma função usada pelas nossas queries.
            cursor.execute("SET search_path TO public")

    return engine


def init_engine(settings: Settings) -> Engine:
    global _engine, _session_factory
    if _engine is None:
        configure_mappers()
        _engine = build_engine(settings)
        _session_factory = sessionmaker(
            bind=_engine,
            # `expire_on_commit=False` para que a entidade continue legível
            # depois do commit. Com o padrão `True`, ler `contract.number`
            # depois de confirmar a transação dispara um SELECT novo — às
            # vezes fora de qualquer transação, o que estoura.
            expire_on_commit=False,
            autoflush=False,
            future=True,
        )
        logger.info("engine de banco inicializado", extra={"env": settings.app_env})
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("engine não inicializado: chame init_engine(settings) antes")
    return _session_factory


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def check_database(engine: Engine) -> bool:
    """Sondagem para o endpoint de readiness."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("sondagem de banco falhou", exc_info=True)
        return False
