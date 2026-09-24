"""Injeção de dependências da API.

O container é explícito (`AppContainer`) em vez de espalhar variáveis de módulo.
O ganho aparece no teste: `app.dependency_overrides` troca qualquer peça —
banco, provedor de notificação, limitador — sem tocar em nenhuma rota.

Sobre o ciclo de vida: cada requisição abre **uma** unidade de trabalho e a
fecha ao terminar, via `yield`. O FastAPI executa o trecho após o `yield`
inclusive quando o handler levanta exceção, que é o que garante o fechamento da
conexão em todo caminho.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.context import current_context
from app.core.logging import get_logger
from app.core.permissions import Permission, permissions_for, role_has
from app.core.rate_limit import NullRateLimiter, RateLimiterPort
from app.domain.entities import User
from app.domain.enums import AuditAction, AuditOutcome
from app.domain.exceptions import AuthenticationError, AuthorizationError, RateLimitError
from app.domain.ports.notifier import NotificationGateway
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.automation_service import AutomationService
from app.services.client_service import ClientService
from app.services.contract_service import ContractService
from app.services.notification_service import NotificationService

logger = get_logger(__name__)


@dataclass
class AppContainer:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    notification_gateway: NotificationGateway
    rate_limiter: RateLimiterPort


_container: AppContainer | None = None


def set_container(container: AppContainer) -> None:
    global _container
    _container = container


def get_container() -> AppContainer:
    if _container is None:
        raise RuntimeError("container não inicializado")
    return _container


def get_app_settings() -> Settings:
    return _container.settings if _container else get_settings()


def get_uow() -> Iterator[SqlAlchemyUnitOfWork]:
    container = get_container()
    with SqlAlchemyUnitOfWork(container.session_factory) as uow:
        yield uow


UowDep = Annotated[SqlAlchemyUnitOfWork, Depends(get_uow)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


# ---------------------------------------------------------------------------
# Serviços
# ---------------------------------------------------------------------------
def get_auth_service(uow: UowDep, settings: SettingsDep) -> AuthService:
    return AuthService(uow, settings)


def get_client_service(uow: UowDep) -> ClientService:
    return ClientService(uow)


def get_contract_service(uow: UowDep) -> ContractService:
    return ContractService(uow)


def get_notification_service(uow: UowDep, settings: SettingsDep) -> NotificationService:
    return NotificationService(uow, get_container().notification_gateway, settings)


def get_automation_service(uow: UowDep, settings: SettingsDep) -> AutomationService:
    return AutomationService(uow, get_container().notification_gateway, settings)


def get_audit_service(uow: UowDep) -> AuditService:
    return AuditService(uow)


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def _extract_bearer(request: Request) -> str:
    """Lê o token do cabeçalho `Authorization`.

    Escrito à mão em vez de usar `OAuth2PasswordBearer` por um motivo: aquele
    utilitário responde 401 com `detail` em inglês e corpo fora do padrão RFC
    7807 usado no resto da API. Duas formas de erro na mesma API é o tipo de
    inconsistência que obriga o cliente a tratar dois casos.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("credencial ausente ou malformada")
    return token.strip()


def get_current_user(request: Request, uow: UowDep, settings: SettingsDep) -> User:
    """Resolve o usuário do access token e **confere o estado atual da conta**.

    A ida ao banco a cada requisição é uma escolha consciente, e o motivo é
    revogação: um token assinado continua válido pelos seus 15 minutos mesmo
    depois de o usuário ser desativado. Sem esta consulta, demitir alguém não
    tem efeito imediato. O custo é uma leitura por chave primária, que o
    PostgreSQL resolve em microssegundos.
    """
    from app.core.security import decode_token  # import tardio: evita ciclo

    token = _extract_bearer(request)
    claims = decode_token(settings, token, expected_type="access")

    user = uow.users.get(claims.subject)
    if user is None or not user.is_active:
        raise AuthenticationError("credencial inválida")

    # O papel vem do banco, não do token. Se o usuário foi rebaixado de ADMIN
    # para OPERATOR, o token na mão dele ainda diz ADMIN — e confiar no token
    # manteria o privilégio até ele expirar.
    #
    # `set_actor` **muta** o contexto em vez de trocá-lo. O motivo está na
    # docstring de `RequestContext`: com `ContextVar.set()`, a alteração não
    # sobrevive à fronteira do threadpool e a auditoria sai sem autor.
    current_context().set_actor(user_id=user.id, email=user.email.value, role=user.role)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require(*permissions: Permission):  # type: ignore[no-untyped-def]
    """Fábrica de dependência de autorização.

    Uso: `dependencies=[Depends(require(Permission.CONTRACT_CREATE))]`.

    Com várias permissões, exige **todas**. A negação é auditada: tentativa de
    acesso negado é justamente o evento que uma investigação procura, e ele não
    aparece em nenhum lugar se o 403 for só uma resposta HTTP.
    """

    def _checker(user: CurrentUser, uow: UowDep) -> User:
        missing = [p for p in permissions if not role_has(user.role, p)]
        if missing:
            AuditService(uow).record_isolated(
                action=AuditAction.ACCESS_DENIED,
                resource_type="permission",
                resource_id=",".join(p.value for p in missing),
                outcome=AuditOutcome.FAILURE,
                details={
                    "papel": user.role.value,
                    "permissoes_exigidas": [p.value for p in permissions],
                },
            )
            logger.warning(
                "acesso negado",
                extra={"papel": user.role.value, "faltando": [p.value for p in missing]},
            )
            raise AuthorizationError("seu perfil não tem permissão para esta operação")
        return user

    return _checker


def current_permissions(user: User) -> list[str]:
    return sorted(p.value for p in permissions_for(user.role))


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def get_rate_limiter() -> RateLimiterPort:
    return _container.rate_limiter if _container else NullRateLimiter()


def enforce_login_rate_limit(request: Request, settings: SettingsDep) -> None:
    """Limite específico do login, por IP.

    Separado do limite geral e bem mais apertado: `/auth/login` é o endpoint que
    aceita credencial, e é onde a força bruta bate. O limite geral de 120/min
    permitiria 120 tentativas de senha por minuto, o que não protege nada.
    """
    if not settings.rate_limit_enabled:
        return
    limiter = get_rate_limiter()
    decision = limiter.check(
        f"login:{client_ip(request, settings)}",
        limit=settings.rate_limit_login_per_minute,
        window_seconds=60,
    )
    if not decision.allowed:
        logger.warning("limite de tentativas de login excedido")
        raise RateLimitError(
            "muitas tentativas de login; aguarde antes de tentar de novo",
            retry_after_seconds=decision.retry_after_seconds,
        )


def client_ip(request: Request, settings: Settings) -> str:
    """IP de origem, respeitando o proxy **apenas quando configurado**.

    `X-Forwarded-For` é um cabeçalho que o cliente envia — ou seja, forjável.
    Confiar nele sem proxy reverso na frente permite que qualquer pessoa escape
    do rate limit trocando o cabeçalho a cada requisição, e ainda envenene o
    campo de IP da auditoria. Por isso o padrão de `TRUST_PROXY_HEADERS` é
    `false`.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # O primeiro da lista é o cliente original; os demais são a cadeia
            # de proxies.
            return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else "desconhecido"


def parse_uuid(value: str, *, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        from app.domain.exceptions import ValidationError

        raise ValidationError(f"{field} inválido") from exc
