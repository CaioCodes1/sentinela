"""Rotas de autenticação."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from app.api.deps import (
    CurrentUser,
    UowDep,
    current_permissions,
    enforce_login_rate_limit,
    get_auth_service,
    require,
)
from app.api.schemas.auth import (
    ChangePasswordRequest,
    CreateUserRequest,
    CurrentUserResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
    UserResponse,
)
from app.api.schemas.common import MessageResponse, ProblemDetail
from app.core.permissions import Permission
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Autenticação"])

AuthDep = Annotated[AuthService, Depends(get_auth_service)]

ERROS_AUTH: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemDetail, "description": "Credenciais inválidas"},
    423: {"model": ProblemDetail, "description": "Conta bloqueada por tentativas"},
    429: {"model": ProblemDetail, "description": "Limite de tentativas excedido"},
}


@router.post(
    "/login",
    response_model=TokenResponse,
    responses=ERROS_AUTH,
    summary="Autentica e devolve o par de tokens",
    # O rate limit específico é dependência da rota, não middleware: o limite
    # geral (120/min) não protegeria contra força bruta de senha.
    dependencies=[Depends(enforce_login_rate_limit)],
)
def login(payload: LoginRequest, service: AuthDep) -> TokenResponse:
    """Autentica por e-mail e senha.

    Falha sempre com a mesma mensagem, qualquer que seja o motivo (e-mail
    inexistente, senha errada, conta desativada) e com tempo de resposta
    semelhante — para não virar um verificador de quais e-mails existem.
    """
    pair = service.authenticate(email=payload.email, password=payload.password)
    return TokenResponse.model_validate(pair)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses=ERROS_AUTH,
    summary="Rotaciona o refresh token",
)
def refresh(payload: RefreshRequest, service: AuthDep) -> TokenResponse:
    """Troca o refresh token por um par novo.

    O token apresentado é **invalidado** na troca. Apresentar de novo um token
    já usado é tratado como indício de roubo: toda a família de tokens daquele
    login é revogada e o evento vai para a auditoria.
    """
    pair = service.refresh(payload.refresh_token)
    return TokenResponse.model_validate(pair)


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoga a sessão atual ou todas",
)
def logout(payload: LogoutRequest, user: CurrentUser, service: AuthDep) -> MessageResponse:
    revoked = service.logout(
        user_id=user.id,
        refresh_token=payload.refresh_token,
        all_sessions=payload.all_sessions,
    )
    return MessageResponse(message=f"{revoked} sessão(ões) revogada(s)")


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Dados e permissões efetivas do usuário autenticado",
)
def me(user: CurrentUser) -> CurrentUserResponse:
    """Devolve o perfil junto com a lista de permissões.

    O cliente usa isso para decidir o que mostrar na interface. Note que é
    informação para a **interface**: a autorização de verdade acontece no
    servidor, em toda rota. Uma interface que esconde o botão não protege nada
    se o endpoint estiver aberto.
    """
    return CurrentUserResponse(
        id=user.id,
        email=str(user.email),
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        permissions=current_permissions(user),
    )


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Troca a própria senha",
)
def change_password(
    payload: ChangePasswordRequest, user: CurrentUser, service: AuthDep
) -> MessageResponse:
    """Exige a senha atual e **encerra todas as sessões** ao concluir."""
    service.change_password(
        user_id=user.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return MessageResponse(message="senha alterada; faça login novamente")


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.USER_CREATE))],
    summary="Cria usuário (somente ADMIN)",
)
def create_user(payload: CreateUserRequest, service: AuthDep, _uow: UowDep) -> UserResponse:
    user = service.create_user(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=payload.role,
    )
    return UserResponse.model_validate(user)
