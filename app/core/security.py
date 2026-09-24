"""Primitivas de segurança: hash de senha e emissão/verificação de JWT.

Nada de criptografia caseira. Este módulo é fino de propósito — ele escolhe
bibliotecas estabelecidas e cuida dos detalhes em que as implementações erram:
comparação em tempo constante, `jti` para revogação, `aud`/`iss` verificados de
verdade, e refresh token guardado como hash.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import Settings
from app.domain.enums import Role
from app.domain.exceptions import AuthenticationError, ValidationError

TokenType = Literal["access", "refresh"]

# bcrypt trunca silenciosamente em 72 bytes. Rejeitar antes é melhor que aceitar
# uma senha de 200 caracteres e validar só os 72 primeiros — o usuário acharia
# que tem uma senha forte que na prática é outra.
BCRYPT_MAX_BYTES = 72

_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"[0-9]")
_SYMBOL = re.compile(r"[^A-Za-z0-9]")

# Hash descartável usado para gastar tempo quando o e-mail não existe. Ver
# `dummy_verify` abaixo.
_DUMMY_HASH = bcrypt.hashpw(b"sentinela-dummy-password", bcrypt.gensalt(rounds=12)).decode()


# ---------------------------------------------------------------------------
# Senhas
# ---------------------------------------------------------------------------
def hash_password(plain: str, *, rounds: int = 12) -> str:
    _reject_oversized(plain)
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Hash corrompido ou em formato desconhecido: é falha de autenticação,
        # não erro 500. Levantar aqui entregaria ao atacante a informação de
        # que aquele registro existe e está quebrado.
        return False


def dummy_verify() -> None:
    """Gasta o mesmo tempo de um `checkpw` real quando o usuário não existe.

    Sem isto, "e-mail inexistente" responde em 1 ms e "senha errada" em 250 ms.
    A diferença é medível de fora e transforma o login num oráculo de quais
    e-mails estão cadastrados — enumeração de usuários por canal lateral de
    tempo. O custo é uma verificação de bcrypt jogada fora por login inválido.
    """
    bcrypt.checkpw(b"sentinela-dummy-password", _DUMMY_HASH.encode())


def _reject_oversized(plain: str) -> None:
    if len(plain.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValidationError(f"senha excede {BCRYPT_MAX_BYTES} bytes, que é o limite do bcrypt")


def validate_password_strength(plain: str, *, min_length: int = 12) -> None:
    """Política de senha. Deliberadamente sem "troque a cada 30 dias".

    Rotação forçada é contraindicada pelo NIST SP 800-63B desde 2017: produz
    `Senha2026!`, `Senha2027!` e o post-it no monitor. O que fica é tamanho,
    variedade e rejeição de sequências óbvias.
    """
    _reject_oversized(plain)
    problems: list[str] = []
    if len(plain) < min_length:
        problems.append(f"mínimo de {min_length} caracteres")
    if not _UPPER.search(plain):
        problems.append("ao menos uma letra maiúscula")
    if not _LOWER.search(plain):
        problems.append("ao menos uma letra minúscula")
    if not _DIGIT.search(plain):
        problems.append("ao menos um dígito")
    if not _SYMBOL.search(plain):
        problems.append("ao menos um símbolo")
    lowered = plain.lower()
    if any(seq in lowered for seq in ("123456", "password", "senha", "qwerty", "admin")):
        problems.append("não pode conter sequência ou palavra óbvia")
    if problems:
        raise ValidationError("senha fraca: " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TokenClaims:
    subject: uuid.UUID
    role: Role
    token_type: TokenType
    jti: uuid.UUID
    family_id: uuid.UUID | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    jti: uuid.UUID
    expires_at: datetime


def _encode(
    *,
    settings: Settings,
    subject: uuid.UUID,
    role: Role,
    token_type: TokenType,
    lifetime: timedelta,
    family_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + lifetime
    jti = uuid.uuid4()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role.value,
        "typ": token_type,
        "jti": str(jti),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    if family_id is not None:
        payload["fam"] = str(family_id)
    if extra:
        payload.update(extra)
    token = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def issue_access_token(settings: Settings, *, subject: uuid.UUID, role: Role) -> IssuedToken:
    return _encode(
        settings=settings,
        subject=subject,
        role=role,
        token_type="access",  # noqa: S106
        lifetime=timedelta(minutes=settings.access_token_ttl_minutes),
    )


def issue_refresh_token(
    settings: Settings, *, subject: uuid.UUID, role: Role, family_id: uuid.UUID
) -> IssuedToken:
    return _encode(
        settings=settings,
        subject=subject,
        role=role,
        token_type="refresh",  # noqa: S106
        lifetime=timedelta(days=settings.refresh_token_ttl_days),
        family_id=family_id,
    )


def decode_token(settings: Settings, token: str, *, expected_type: TokenType) -> TokenClaims:
    """Valida assinatura, validade, emissor, audiência **e tipo**.

    A checagem de `typ` é a que costuma faltar. Sem ela, o refresh token — que
    vive sete dias — é aceito como access token, e a expiração curta do access
    vira decoração.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["exp", "iat", "nbf", "sub", "jti", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expirado") from exc
    except jwt.InvalidTokenError as exc:
        # Uma mensagem só para assinatura inválida, audiência errada, algoritmo
        # trocado e JSON malformado. Detalhar ajudaria a calibrar o ataque.
        raise AuthenticationError("token inválido") from exc

    if payload.get("typ") != expected_type:
        raise AuthenticationError("token inválido")

    try:
        return TokenClaims(
            subject=uuid.UUID(payload["sub"]),
            role=Role(payload["role"]),
            token_type=expected_type,
            jti=uuid.UUID(payload["jti"]),
            family_id=uuid.UUID(payload["fam"]) if payload.get("fam") else None,
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("token inválido") from exc


def hash_refresh_token(token: str) -> str:
    """SHA-256 do token, que é o que vai para o banco.

    Não é bcrypt de propósito: o refresh token já é 256 bits de entropia gerada
    por nós, não uma senha escolhida por humano. Não há o que adivinhar por
    dicionário, e um bcrypt por chamada de `/refresh` seria custo puro. O que
    importa é que um vazamento do banco não entregue tokens utilizáveis.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_api_secret(nbytes: int = 32) -> str:
    """Segredo aleatório para `JWT_SECRET` e afins. Usa `secrets`, nunca `random`."""
    return secrets.token_urlsafe(nbytes)
