"""Middlewares: contexto da requisição, cabeçalhos de segurança e rate limit.

Ordem de registro importa e está documentada em `app/main.py`.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import Settings
from app.core.context import RequestContext, new_request_id, use_context
from app.core.errors import problem
from app.core.logging import get_logger
from app.core.rate_limit import RateLimiterPort

logger = get_logger(__name__)

# Caminhos da documentação. Precisam de uma CSP diferente — ver `SecurityHeaders`.
DOCS_PATHS = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")

# Tamanho máximo de corpo aceito. Nenhum endpoint desta API recebe mais que
# alguns KB; aceitar 100 MB significa deixar qualquer um alocar 100 MB de
# memória do processo com uma requisição.
MAX_BODY_BYTES = 256 * 1024


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Abre o contexto, mede a duração e devolve o `X-Request-Id`.

    O id vem do cliente quando ele manda um (`X-Request-Id`), o que permite
    correlacionar chamadas que atravessam vários serviços. Ele é **saneado**
    antes de ser usado: é texto de fora, vai parar no log e na auditoria, e um
    valor com quebra de linha permitiria forjar linhas de log inteiras.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.api.deps import client_ip

        incoming = request.headers.get("X-Request-Id", "")
        request_id = _sanitize_request_id(incoming) or new_request_id()

        context = RequestContext(
            request_id=request_id,
            ip_address=client_ip(request, self._settings),
            user_agent=request.headers.get("User-Agent", "")[:300] or None,
        )

        started = time.perf_counter()
        with use_context(context):
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.headers["X-Request-Id"] = request_id

            # O caminho vem de `request.url.path`, não da rota bruta: o
            # template (`/clients/{client_id}`) evita que um id vire dimensão
            # de log com cardinalidade infinita.
            route = request.scope.get("route")
            path_template = getattr(route, "path", request.url.path)

            logger.info(
                "%s %s -> %s",
                request.method,
                path_template,
                response.status_code,
                extra={
                    "http_method": request.method,
                    "http_path": path_template,
                    "status_code": response.status_code,
                    "duration_ms": round(elapsed_ms, 2),
                },
            )
            return response


def _sanitize_request_id(value: str) -> str:
    """Só alfanuméricos, hífen e sublinhado, no máximo 64 caracteres."""
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_")[:64]
    return cleaned


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Cabeçalhos de defesa em profundidade.

    O ponto delicado é a **CSP**. O valor correto para uma API que só devolve
    JSON é `default-src 'none'`: ela não serve HTML, não carrega script, não
    embute nada. Só que o Swagger UI **é** HTML servido pela mesma aplicação, e
    com essa CSP ele abre em branco — o CSS e o JS da própria documentação são
    bloqueados pela política. O sintoma não aparece em nenhum teste
    automatizado, porque teste de API não renderiza página.

    Solução: CSP estrita para tudo, com uma exceção declarada para os caminhos
    da documentação. É melhor que afrouxar a política inteira por causa do
    `/docs`.
    """

    def __init__(self, app: ASGIApp, *, is_production: bool) -> None:
        super().__init__(app)
        self._is_production = is_production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path

        if path.startswith(DOCS_PATHS):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "img-src 'self' data: https://fastapi.tiangolo.com; "
                "script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "connect-src 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Desliga APIs de dispositivo que uma API REST nunca usa. Barato, e
        # evita surpresa se um dia algum HTML for servido daqui.
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # Nunca guardar resposta de API em cache compartilhado: a mesma URL
        # devolve conteúdo diferente por usuário, e um proxy que cacheie o
        # `/auth/me` de alguém serve esse perfil para o próximo.
        response.headers["Cache-Control"] = "no-store"

        if self._is_production:
            # HSTS só em produção: em desenvolvimento o navegador passaria a
            # forçar HTTPS em `localhost` e quebraria o acesso local — com
            # efeito persistente, porque o navegador memoriza a política.
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # O Starlette não envia `Server`, mas um servidor à frente pode. Anunciar
        # produto e versão entrega de graça a lista de CVEs aplicáveis.
        #
        # `MutableHeaders` do Starlette não tem `pop`: a remoção é por `del`,
        # com a checagem de existência antes.
        if "server" in response.headers:
            del response.headers["server"]
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Recusa corpo acima do teto, pelo `Content-Length`.

    É uma barreira barata, não uma garantia: um cliente pode omitir o
    `Content-Length` e usar `Transfer-Encoding: chunked`. A garantia real é o
    limite configurado no proxy reverso, que este middleware complementa para
    o caso de a aplicação rodar exposta.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        declared = request.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return problem(
                status_code=413,
                code="payload_too_large",
                detail=f"Corpo da requisição acima do limite de {MAX_BODY_BYTES} bytes.",
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limite geral por origem, aplicado antes de qualquer rota.

    Fica em middleware, e não em dependência, para que uma requisição barrada
    não chegue a abrir conexão com o banco. Quando o limite serve para conter
    um abuso, todo trabalho que ele evita conta.

    A chave é o IP. Com usuário autenticado o ideal seria a identidade — mas
    resolver a identidade exige decodificar o token e ir ao banco, que é
    justamente o custo que se quer evitar aqui.
    """

    def __init__(self, app: ASGIApp, *, limiter: RateLimiterPort, settings: Settings) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.api.deps import client_ip

        if not self._settings.rate_limit_enabled or request.url.path in (
            "/health",
            "/health/live",
        ):
            return await call_next(request)

        ip = client_ip(request, self._settings)
        decision = self._limiter.check(
            f"geral:{ip}",
            limit=self._settings.rate_limit_default_per_minute,
            window_seconds=60,
        )
        if not decision.allowed:
            logger.warning("limite geral de requisições excedido")
            return problem(
                status_code=429,
                code="rate_limited",
                detail="Limite de requisições excedido.",
                headers={
                    "Retry-After": str(decision.retry_after_seconds),
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response
