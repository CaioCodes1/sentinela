"""Tradução de erro para resposta HTTP, num lugar só.

Formato de saída: **RFC 7807 (Problem Details)** — `type`, `title`, `status`,
`detail`, mais `code` e `request_id` nossos. É o padrão que ferramentas de API
corporativa já sabem ler, e evita que cada endpoint invente o seu envelope.

A regra que governa este arquivo: **a resposta de erro nunca conta ao cliente
mais do que ele precisa para corrigir a chamada.** Um `IntegrityError` do
psycopg traz nome de tabela, de restrição e às vezes o valor que colidiu —
mapa gratuito do schema para quem está sondando. Aqui ele vira "conflito com o
estado atual do recurso", e o detalhe real vai para o log, amarrado ao
`request_id` que o cliente recebe. O suporte cruza os dois; o atacante, não.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import current_context
from app.core.logging import get_logger
from app.domain.exceptions import (
    AccountLockedError,
    AuthenticationError,
    AuthorizationError,
    BusinessRuleError,
    ConcurrencyError,
    ConflictError,
    DomainError,
    ExternalServiceError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

logger = get_logger(__name__)

# 422 escrito como número, de propósito: o Starlette renomeou a constante
# (`HTTP_422_UNPROCESSABLE_ENTITY` -> `..._CONTENT`) e manter o nome antigo
# emite DeprecationWarning, enquanto o novo quebra em versões anteriores.
# O código numérico é estável desde a RFC 4918.
HTTP_422 = 422

PROBLEM_CONTENT_TYPE = "application/problem+json"

_STATUS_BY_ERROR: list[tuple[type[DomainError], int]] = [
    # A ordem é significativa: a primeira classe compatível vence, e
    # AccountLockedError herda de AuthenticationError.
    (AccountLockedError, status.HTTP_423_LOCKED),
    (RateLimitError, status.HTTP_429_TOO_MANY_REQUESTS),
    (AuthenticationError, status.HTTP_401_UNAUTHORIZED),
    (AuthorizationError, status.HTTP_403_FORBIDDEN),
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ConflictError, status.HTTP_409_CONFLICT),
    (ConcurrencyError, status.HTTP_409_CONFLICT),
    (BusinessRuleError, HTTP_422),
    (ValidationError, status.HTTP_400_BAD_REQUEST),
    (ExternalServiceError, status.HTTP_502_BAD_GATEWAY),
]

_TITLES = {
    400: "Requisição inválida",
    401: "Não autenticado",
    403: "Acesso negado",
    404: "Recurso não encontrado",
    409: "Conflito com o estado atual",
    422: "Regra de negócio violada",
    423: "Conta bloqueada",
    429: "Limite de requisições excedido",
    500: "Erro interno",
    502: "Serviço externo indisponível",
    503: "Serviço temporariamente indisponível",
}


def problem(
    *,
    status_code: int,
    code: str,
    detail: str,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://sentinela.dev/errors/{code}",
        "title": _TITLES.get(status_code, "Erro"),
        "status": status_code,
        "code": code,
        "detail": detail,
        "request_id": current_context().request_id,
    }
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def _status_for(exc: DomainError) -> int:
    for error_type, code in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return code
    return status.HTTP_400_BAD_REQUEST


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        status_code = _status_for(exc)
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        if status_code == status.HTTP_401_UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"

        # 4xx é uso incorreto da API pelo cliente: informação, não incidente.
        # Só 401/403 sobem para WARNING, porque em volume viram sinal de ataque.
        log = logger.warning if status_code in (401, 403, 423, 429) else logger.info
        log(
            "erro de domínio: %s",
            exc.code,
            extra={"status_code": status_code, "error_code": exc.code},
        )
        return problem(
            status_code=status_code,
            code=exc.code,
            detail=exc.message,
            extra={"details": exc.details} if exc.details else None,
            headers=headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Erro de schema do Pydantic, reescrito.

        O erro cru do Pydantic inclui `input`, ou seja, **o valor rejeitado**.
        Num `POST /auth/login` malformado, isso significa devolver a senha
        digitada dentro do corpo da resposta de erro — que costuma ser logada
        pelo cliente. Aqui fica só onde e por quê, nunca o quê.
        """
        errors = [
            {
                "campo": ".".join(str(part) for part in error["loc"][1:]) or "corpo",
                "erro": error["msg"],
                "tipo": error["type"],
            }
            for error in exc.errors()
        ]
        return problem(
            status_code=HTTP_422,
            code="schema_validation_error",
            detail="A requisição não passou na validação de schema.",
            extra={"errors": errors},
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        # `exc.orig` traz a mensagem do PostgreSQL, com nome de restrição e
        # valor. Vai para o log; jamais para a resposta.
        logger.warning("violação de integridade no banco", exc_info=exc)
        return problem(
            status_code=status.HTTP_409_CONFLICT,
            code="conflict",
            detail="A operação conflita com o estado atual dos dados.",
        )

    @app.exception_handler(OperationalError)
    async def _operational(_: Request, exc: OperationalError) -> JSONResponse:
        logger.error("banco de dados indisponível", exc_info=exc)
        return problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="database_unavailable",
            detail="Serviço temporariamente indisponível. Tente novamente em instantes.",
        )

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("erro inesperado de persistência", exc_info=exc)
        return problem(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            detail="Erro interno ao processar a requisição.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            detail=str(exc.detail),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Rede de segurança.

        Sem este handler, uma exceção não prevista sobe até o Starlette, que em
        modo debug devolve o traceback na resposta — caminho de arquivo, trecho
        de código e variáveis locais. O `request_id` é o que liga esta resposta
        opaca à entrada de log que tem tudo.
        """
        logger.exception("exceção não tratada", exc_info=exc)
        return problem(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            detail="Erro interno ao processar a requisição.",
        )
