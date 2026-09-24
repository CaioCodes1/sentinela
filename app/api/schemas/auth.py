"""Schemas de autenticação."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.api.schemas.common import OutputModel, StrictModel
from app.domain.enums import Role


class LoginRequest(StrictModel):
    email: str = Field(max_length=254, examples=["admin@sentinela.local"])
    # `min_length=1` e não a política de senha completa: a validação de força
    # vale para *definir* senha, não para *conferir*. Recusar no schema uma
    # senha curta num login entregaria que a política mudou — e, pior, daria
    # mensagem diferente para senha fraca e senha errada.
    password: str = Field(min_length=1, max_length=200, repr=False)

    @field_validator("password")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("senha contém caractere inválido")
        return value


class TokenResponse(OutputModel):
    access_token: str
    refresh_token: str
    # S105 aqui é falso positivo: "Bearer" é o esquema de autenticação do
    # RFC 6750, não uma senha embutida.
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int = Field(description="Validade do access token, em segundos")
    role: Role


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=4096, repr=False)


class LogoutRequest(StrictModel):
    refresh_token: str | None = Field(default=None, max_length=4096, repr=False)
    all_sessions: bool = Field(default=False, description="Revoga todas as sessões do usuário")


class ChangePasswordRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=200, repr=False)
    new_password: str = Field(min_length=12, max_length=200, repr=False)


class CreateUserRequest(StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=200, repr=False)
    full_name: str = Field(min_length=3, max_length=120)
    role: Role


class UserResponse(OutputModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime

    @field_validator("email", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> str:
        return str(value)


class CurrentUserResponse(UserResponse):
    permissions: list[str] = Field(
        description="Permissões efetivas do papel, na forma 'recurso:ação'"
    )
