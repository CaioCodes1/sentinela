"""Fixtures dos testes de integração.

Aqui a aplicação sobe de verdade: FastAPI, SQLAlchemy, PostgreSQL e as
migrations. O único componente substituído é o **provedor externo de
notificação** — trocado por um dublê, porque depender de rede numa suíte
automatizada produz falha intermitente, e falha intermitente ensina o time a
reexecutar o teste em vez de investigar.

O que só estes testes conseguem provar: índice único, `ON CONFLICT`, gatilho de
imutabilidade, trava otimista, advisory lock e o formato real das respostas
HTTP.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.enums import Role
from tests.fakes import FakeNotificationGateway

pytestmark = pytest.mark.integration

SENHA_PADRAO = "Sentinela#2026-Forte"


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    """Configuração apontando para o banco de teste."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL não definida")
    return settings.model_copy(
        update={
            "database_url": settings.database_url.__class__(url),
            "scheduler_enabled": False,
            "rate_limit_enabled": False,
            "docs_enabled": True,
        }
    )


@pytest.fixture
def notifier() -> FakeNotificationGateway:
    return FakeNotificationGateway()


@pytest.fixture
def api(api_settings: Settings, engine, notifier) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    """Cliente HTTP sobre a aplicação real.

    O `with TestClient(...)` é obrigatório: sem ele o `lifespan` não roda, o
    container nunca é montado e toda requisição falha com "container não
    inicializado". É o erro nº 1 ao testar FastAPI com ciclo de vida.
    """
    from app.api.deps import get_container
    from app.infrastructure.db.session import dispose_engine
    from app.main import create_app

    dispose_engine()  # descarta engine de um teste anterior
    app = create_app(api_settings)

    with TestClient(app) as client:
        get_container().notification_gateway = notifier
        yield client

    dispose_engine()


@pytest.fixture
def criar_usuario(api_settings: Settings, engine):  # type: ignore[no-untyped-def]
    """Cria usuários direto no banco, sem passar pela API.

    Criar por HTTP exigiria um ADMIN já existente — e o primeiro ADMIN não
    pode nascer de uma rota pública. Na aplicação real quem faz isso é o CLI.
    """
    from sqlalchemy.orm import sessionmaker

    from app.core.security import hash_password
    from app.domain.entities import User
    from app.domain.value_objects import EmailAddress

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def _criar(*, email: str, papel: Role, senha: str = SENHA_PADRAO, ativo: bool = True) -> User:
        user = User(
            email=EmailAddress.parse(email),
            password_hash=hash_password(senha, rounds=api_settings.bcrypt_rounds),
            role=papel,
            full_name=f"Usuário {papel.value}",
            is_active=ativo,
        )
        with factory() as session:
            session.add(user)
            session.commit()
        return user

    return _criar


@pytest.fixture
def admin(criar_usuario):  # type: ignore[no-untyped-def]
    return criar_usuario(email="admin@sentinela.local", papel=Role.ADMIN)


@pytest.fixture
def operador(criar_usuario):  # type: ignore[no-untyped-def]
    return criar_usuario(email="operador@sentinela.local", papel=Role.OPERATOR)


@pytest.fixture
def auditor(criar_usuario):  # type: ignore[no-untyped-def]
    return criar_usuario(email="auditor@sentinela.local", papel=Role.AUDITOR)


@pytest.fixture
def autenticar(api: TestClient):  # type: ignore[no-untyped-def]
    """Faz login e devolve o cabeçalho pronto."""

    def _login(email: str, senha: str = SENHA_PADRAO) -> dict[str, str]:
        resposta = api.post("/api/v1/auth/login", json={"email": email, "password": senha})
        assert resposta.status_code == 200, resposta.text
        return {"Authorization": f"Bearer {resposta.json()['access_token']}"}

    return _login


@pytest.fixture
def cabecalho_admin(autenticar, admin):  # type: ignore[no-untyped-def]
    return autenticar(str(admin.email))


@pytest.fixture
def cabecalho_operador(autenticar, operador):  # type: ignore[no-untyped-def]
    return autenticar(str(operador.email))


@pytest.fixture
def cabecalho_auditor(autenticar, auditor):  # type: ignore[no-untyped-def]
    return autenticar(str(auditor.email))


@pytest.fixture
def cliente_criado(api: TestClient, cabecalho_operador):  # type: ignore[no-untyped-def]
    resposta = api.post(
        "/api/v1/clients",
        headers=cabecalho_operador,
        json={
            "tax_id": "12.345.678/0001-95",
            "legal_name": "Aurora Serviços Financeiros Ltda",
            "email": "financeiro@aurora.exemplo.br",
            "phone": "1133334444",
        },
    )
    assert resposta.status_code == 201, resposta.text
    return resposta.json()


@pytest.fixture
def contrato_criado(api: TestClient, cabecalho_operador, cliente_criado):  # type: ignore[no-untyped-def]
    from datetime import date, timedelta

    hoje = date.today()
    resposta = api.post(
        "/api/v1/contracts",
        headers=cabecalho_operador,
        json={
            "client_id": cliente_criado["id"],
            "number": "CT-2026-0001",
            "monthly_amount": "1500.00",
            "start_date": (hoje - timedelta(days=30)).isoformat(),
            "end_date": (hoje + timedelta(days=335)).isoformat(),
            "due_day": 10,
            "description": "Contrato de prestação de serviços",
        },
    )
    assert resposta.status_code == 201, resposta.text
    return resposta.json()
