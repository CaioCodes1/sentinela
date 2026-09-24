"""Ponto de entrada da aplicação.

Este arquivo faz três coisas e nada além disso: monta o container de
dependências, registra middlewares na ordem certa e liga as rotas. Toda a
lógica mora nas camadas de baixo — se um dia for preciso expor a mesma
aplicação por gRPC ou por linha de comando, nada daqui é reaproveitado e nada
do resto precisa mudar.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import AppContainer, get_container, set_container
from app.api.docs import router as docs_router
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import InMemorySlidingWindowLimiter
from app.infrastructure.db.session import dispose_engine, get_session_factory, init_engine
from app.infrastructure.gateways.http_notifier import HttpNotificationGateway
from app.jobs.scheduler import start_scheduler, stop_scheduler

logger = get_logger(__name__)

DESCRICAO = """
Plataforma de automação de contratos, mensalidades e renovações.

**Autenticação:** todos os endpoints sob `/api/v1`, exceto `/auth/login` e
`/auth/refresh`, exigem `Authorization: Bearer <access_token>`.

**Perfis:** `ADMIN` (tudo), `OPERATOR` (operação do dia a dia, sem auditoria e
sem gestão de usuários) e `AUDITOR` (somente leitura, incluindo a trilha de
auditoria). A matriz viva está em `GET /api/v1/admin/permissions`.

**Erros:** todas as respostas de falha seguem o formato RFC 7807
(`application/problem+json`) e trazem um `request_id` que aparece no log do
servidor.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    # **Uma** instância de limitador para o processo inteiro. Duas instâncias
    # (uma no middleware, outra no container) contariam separadamente, e o
    # limite efetivo seria o dobro do configurado — sem nenhum sintoma
    # visível além de o teste de rate limit falhar de vez em quando.
    rate_limiter = InMemorySlidingWindowLimiter()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        engine = init_engine(settings)
        gateway = HttpNotificationGateway(settings)
        set_container(
            AppContainer(
                settings=settings,
                engine=engine,
                session_factory=get_session_factory(),
                notification_gateway=gateway,
                rate_limiter=rate_limiter,
            )
        )
        if settings.scheduler_enabled:
            start_scheduler(settings)
        logger.info(
            "aplicação iniciada",
            extra={"env": settings.app_env, "versao": settings.app_version},
        )
        try:
            yield
        finally:
            # Encerramento ordenado: parar o agendador antes de fechar o pool
            # evita que um job disparado no último segundo encontre o banco já
            # desconectado e registre uma falha que não é falha.
            stop_scheduler()
            gateway.close()
            dispose_engine()
            logger.info("aplicação encerrada")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRICAO,
        lifespan=lifespan,
        # Documentação fechada em produção: ela enumera todos os endpoints,
        # schemas e exemplos para quem não está autenticado — um mapa pronto
        # para quem estiver procurando superfície de ataque.
        # `docs_url=None` mesmo com a documentação ligada: a página do Swagger
        # é servida por `app/api/docs.py`, para que a CSP possa declarar o hash
        # do script que ela embute. Ver `SecurityHeadersMiddleware`.
        docs_url=None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        contact={"name": "Sentinela", "url": "https://github.com/CaioCodes1"},
        license_info={"name": "MIT"},
    )

    # ------------------------------------------------------------------
    # A ordem dos middlewares importa, e o Starlette os executa na ordem
    # **inversa** do registro: o último a ser adicionado é o primeiro a ver a
    # requisição. Daí esta sequência:
    #
    #   1. RequestContext   (registrado por último -> roda primeiro)
    #   2. RateLimit        -> barra antes de qualquer trabalho
    #   3. BodySizeLimit    -> recusa corpo grande antes de ler
    #   4. SecurityHeaders  -> carimba a resposta na volta
    #   5. CORS             (registrado primeiro -> roda por último)
    #
    # O contexto precisa ser o primeiro porque o log do rate limit já usa o
    # `request_id`. Se a ordem fosse invertida, a requisição barrada apareceria
    # no log sem identificação — justamente a que se quer rastrear.
    # ------------------------------------------------------------------
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
            max_age=600,
        )

    app.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_production)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=rate_limiter,
        settings=settings,
    )
    app.add_middleware(RequestContextMiddleware, settings=settings)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router)
    if settings.docs_enabled:
        app.include_router(docs_router)

    return app


app = create_app()


__all__ = ["app", "create_app", "get_container"]
