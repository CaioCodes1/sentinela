"""Fixtures compartilhadas.

Divisão da suíte:

- `tests/unit/` — sem banco, sem rede. Rodam sempre, em segundos.
- `tests/integration/` — exigem PostgreSQL em `TEST_DATABASE_URL`. Se a
  variável não estiver definida, são **ignorados com uma mensagem que diz o que
  falta**, em vez de falharem com erro de conexão. A diferença importa: teste
  vermelho por falta de infraestrutura ensina o time a ignorar vermelho.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from dotenv import load_dotenv

from app.core.config import Settings
from app.domain.entities import Client, Contract, User
from app.domain.enums import Role
from app.domain.value_objects import EmailAddress, Money, TaxId
from tests.fakes import FakeNotificationGateway, FakeUnitOfWork

# Carrega o `.env` para o ambiente do processo.
#
# O `Settings` do pydantic-settings lê o `.env` sozinho, mas `os.getenv` não —
# e é `os.getenv` que decide se os testes de integração rodam ou são pulados.
# Sem esta linha, quem tem `TEST_DATABASE_URL` no `.env` vê 0 teste de
# integração executado e nenhuma explicação do porquê.
# `override=False`: variável já exportada no shell (ou no CI) tem prioridade.
load_dotenv(".env", override=False)

# Documentos válidos pelo dígito verificador, usados em toda a suíte.
CPF_VALIDO = "529.982.247-25"
CNPJ_VALIDO = "12.345.678/0001-95"


@pytest.fixture
def settings() -> Settings:
    """Configuração de teste, independente do `.env` da máquina.

    `bcrypt_rounds=4` é o menor valor aceito pela biblioteca e derruba o custo
    de cada hash de ~250 ms para ~1 ms. Numa suíte com dezenas de logins, isso
    é a diferença entre 3 segundos e um minuto. **Nunca** em produção: o valor
    baixo é justamente o que torna a quebra offline viável.
    """
    return Settings(
        app_env="test",
        jwt_secret="segredo-de-teste-com-tamanho-suficiente-0123456789",
        notifier_api_key="chave-de-teste",
        bcrypt_rounds=4,
        access_token_ttl_minutes=15,
        refresh_token_ttl_days=7,
        login_max_attempts=3,
        login_lockout_minutes=15,
        rate_limit_enabled=False,
        scheduler_enabled=False,
        notifier_max_attempts=3,
        notifier_backoff_base_seconds=0.001,
        expiration_alert_days=[30, 15, 7, 1],
    )


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def gateway() -> FakeNotificationGateway:
    return FakeNotificationGateway()


@pytest.fixture
def admin_user(settings: Settings) -> User:
    from app.core.security import hash_password

    return User(
        email=EmailAddress.parse("admin@sentinela.local"),
        password_hash=hash_password("Adm!nSentinela2026", rounds=settings.bcrypt_rounds),
        role=Role.ADMIN,
        full_name="Administrador de Teste",
    )


@pytest.fixture
def client_entity() -> Client:
    return Client(
        tax_id=TaxId.parse(CNPJ_VALIDO),
        legal_name="Aurora Serviços Financeiros Ltda",
        email=EmailAddress.parse("financeiro@aurora.exemplo.br"),
        phone="1133334444",
    )


@pytest.fixture
def contract_factory(client_entity: Client):  # type: ignore[no-untyped-def]
    """Fábrica de contratos com vencimento relativo a hoje.

    Datas relativas, não fixas: um contrato que vence em `2026-10-01` deixa de
    estar "a 30 dias do vencimento" no dia seguinte, e o teste que dependia
    disso começa a falhar sozinho semanas depois — sem que ninguém tenha
    mudado uma linha de código.
    """

    def _make(
        *,
        dias_para_vencer: int = 30,
        numero: str | None = None,
        valor: str = "1000.00",
        due_day: int = 10,
        auto_renew: bool = False,
        client_id: uuid.UUID | None = None,
    ) -> Contract:
        fim = date.today() + timedelta(days=dias_para_vencer)
        return Contract(
            client_id=client_id or client_entity.id,
            number=numero or f"CT-{uuid.uuid4().hex[:8].upper()}",
            monthly_amount=Money.parse(Decimal(valor)),
            start_date=fim - timedelta(days=365),
            end_date=fim,
            due_day=due_day,
            auto_renew=auto_renew,
        )

    return _make


# ---------------------------------------------------------------------------
# Integração
# ---------------------------------------------------------------------------
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Ignora os testes de integração quando não há banco configurado."""
    if os.getenv("TEST_DATABASE_URL"):
        return
    motivo = pytest.mark.skip(
        reason=(
            "TEST_DATABASE_URL não definida. Suba o banco "
            "(`docker compose up -d postgres`) e defina a variável para rodar "
            "os testes de integração."
        )
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(motivo)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL não definida")
    return url


@pytest.fixture(scope="session")
def engine(database_url: str):  # type: ignore[no-untyped-def]
    """Engine da sessão de teste, com o schema criado pelas **migrations**.

    O schema vem do Alembic, e não de `metadata.create_all()`. A diferença é
    decisiva: `create_all` cria as tabelas a partir do código Python e **pula**
    tudo que só existe nas migrations — o gatilho de imutabilidade da
    auditoria, o índice único sobre `lower(email)`, os comentários. A suíte
    passaria sem nunca exercitar as proteções mais importantes do banco.

    Rodar as migrations também testa as migrations.
    """
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from alembic import command
    from app.infrastructure.db.mappers import configure_mappers

    configure_mappers()
    engine = create_engine(url=database_url, future=True)

    with engine.begin() as connection:
        # Estado limpo a cada sessão de teste: `DROP SCHEMA ... CASCADE` remove
        # tabelas, gatilhos, funções e a marca de versão do Alembic de uma vez.
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):  # type: ignore[no-untyped-def]
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@pytest.fixture(autouse=True)
def _limpar_tabelas(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    """Zera as tabelas entre testes de integração.

    `TRUNCATE ... CASCADE` e não `DELETE`: além de ser muito mais rápido,
    `TRUNCATE` **não dispara gatilho de linha**, que é o único jeito de limpar
    `audit_logs` — a tabela recusa `DELETE` por construção. Este é exatamente o
    ponto em que a proteção de produção atrapalharia o teste se a saída não
    tivesse sido pensada junto com ela.
    """
    yield
    if "integration" not in request.keywords:
        return
    from sqlalchemy import text

    engine = request.getfixturevalue("engine")
    tabelas = (
        "audit_logs, contract_events, notifications, installments, "
        "contracts, clients, refresh_tokens, job_runs, users"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tabelas} RESTART IDENTITY CASCADE"))
