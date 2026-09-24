"""Autenticação: login, renovação de token, logout e criação de usuário.

O ponto não óbvio deste arquivo é a **rotação de refresh token com detecção de
reúso**, explicada em `refresh()`. É o que separa "tem JWT" de "tem sessão
defensável".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.context import RequestContext, current_context
from app.core.logging import get_logger
from app.core.security import (
    decode_token,
    dummy_verify,
    hash_password,
    hash_refresh_token,
    issue_access_token,
    issue_refresh_token,
    validate_password_strength,
    verify_password,
)
from app.domain.entities import RefreshToken, User
from app.domain.enums import AuditAction, AuditOutcome, Role
from app.domain.exceptions import (
    AccountLockedError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
)
from app.domain.ports.uow import UnitOfWork
from app.domain.value_objects import EmailAddress
from app.services.audit_service import AuditService

logger = get_logger(__name__)

# Mensagem única para "e-mail não existe", "senha errada" e "conta desativada".
# Diferenciá-las transforma o login num verificador de e-mails cadastrados —
# informação que vale ouro para quem monta uma lista de alvos de phishing.
GENERIC_LOGIN_ERROR = "credenciais inválidas"


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    role: Role


class AuthService:
    def __init__(self, uow: UnitOfWork, settings: Settings) -> None:
        self._uow = uow
        self._settings = settings
        self._audit = AuditService(uow)

    # -- Login -------------------------------------------------------------
    def authenticate(self, *, email: str, password: str) -> TokenPair:
        context = current_context()
        try:
            parsed = EmailAddress.parse(email)
        except Exception:
            dummy_verify()
            self._record_login_failure(email, "email_malformado", context)
            raise AuthenticationError(GENERIC_LOGIN_ERROR) from None

        user = self._uow.users.get_by_email(parsed.value)

        if user is None:
            # Gasta o mesmo tempo de um bcrypt real antes de recusar. Sem isso,
            # a diferença de latência entre "usuário existe" e "não existe"
            # responde a pergunta que a mensagem genérica recusou a responder.
            dummy_verify()
            self._record_login_failure(parsed.value, "usuario_inexistente", context)
            raise AuthenticationError(GENERIC_LOGIN_ERROR)

        now = datetime.now(UTC)

        if user.is_locked(now):
            # Aqui a mensagem é específica de propósito: a conta é do titular,
            # ele precisa saber por que não entra, e quem disparou o bloqueio
            # já sabia que a conta existe.
            self._record_login_failure(parsed.value, "conta_bloqueada", context, user=user)
            raise AccountLockedError("conta temporariamente bloqueada por excesso de tentativas")

        if not verify_password(password, user.password_hash):
            locked_now = user.register_failed_login(
                max_attempts=self._settings.login_max_attempts,
                lockout_minutes=self._settings.login_lockout_minutes,
                now=now,
            )
            # O incremento do contador precisa sobreviver, e o fluxo termina em
            # exceção — que faria rollback. Por isso este commit explícito
            # **antes** de levantar: sem ele, o bloqueio por tentativas nunca
            # aconteceria, porque o contador voltaria a zero a cada erro.
            self._audit.record(
                action=AuditAction.LOGIN_FAILURE,
                resource_type="user",
                resource_id=user.id,
                outcome=AuditOutcome.FAILURE,
                details={"motivo": "senha_incorreta", "tentativas": user.failed_login_attempts},
                context=context,
            )
            if locked_now:
                self._audit.record(
                    action=AuditAction.ACCOUNT_LOCKED,
                    resource_type="user",
                    resource_id=user.id,
                    outcome=AuditOutcome.FAILURE,
                    details={"bloqueado_ate": user.locked_until},
                    context=context,
                )
            self._uow.commit()
            raise AuthenticationError(GENERIC_LOGIN_ERROR)

        if not user.is_active:
            self._record_login_failure(parsed.value, "conta_desativada", context, user=user)
            raise AuthenticationError(GENERIC_LOGIN_ERROR)

        user.register_successful_login(now)
        pair = self._issue_pair(user, context=context, family_id=uuid.uuid4())
        self._audit.record(
            action=AuditAction.LOGIN_SUCCESS,
            resource_type="user",
            resource_id=user.id,
            details={"papel": user.role.value},
            context=context.set_actor(user_id=user.id, email=user.email.value, role=user.role),
        )
        self._uow.commit()
        return pair

    # -- Renovação ---------------------------------------------------------
    def refresh(self, refresh_token: str) -> TokenPair:
        """Troca o refresh token por um par novo, **invalidando o antigo**.

        Rotação com detecção de reúso, em três partes:

        1. Cada `/refresh` bem-sucedido revoga o token apresentado e emite
           outro. Um refresh token vale **uma vez**.
        2. Todos os tokens nascidos do mesmo login compartilham um `family_id`.
        3. Se um token **já revogado** for apresentado, só há duas explicações:
           ou ele foi roubado e o ladrão está usando, ou ele foi roubado e o
           titular já usou. Em ambos os casos há uma cópia na mão errada —
           então a família inteira é revogada e as duas partes são forçadas a
           logar de novo.

        Sem a rotação, um refresh token capturado vale sete dias de acesso sem
        deixar rastro. Com ela, o segundo uso denuncia o roubo e derruba a
        sessão. O incômodo de um logout inesperado é o preço, e é barato.
        """
        context = current_context()
        claims = decode_token(self._settings, refresh_token, expected_type="refresh")
        token_hash = hash_refresh_token(refresh_token)
        stored = self._uow.refresh_tokens.get_by_hash(token_hash)

        if stored is None:
            # Assinatura válida mas sem registro: token de antes de um logout
            # global, ou de uma base restaurada. Não dá para identificar a
            # família, então só se recusa.
            self._audit.record_isolated(
                action=AuditAction.TOKEN_REUSE_DETECTED,
                resource_type="refresh_token",
                resource_id=str(claims.jti),
                details={"motivo": "token_desconhecido", "usuario": str(claims.subject)},
                context=context,
            )
            raise AuthenticationError("token inválido")

        now = datetime.now(UTC)

        if stored.revoked_at is not None:
            revoked_count = self._uow.refresh_tokens.revoke_family(stored.family_id, now=now)
            self._audit.record(
                action=AuditAction.TOKEN_REUSE_DETECTED,
                resource_type="refresh_token",
                resource_id=str(stored.id),
                outcome=AuditOutcome.FAILURE,
                details={
                    "motivo": "reuso_de_token_revogado",
                    "familia": str(stored.family_id),
                    "tokens_revogados": revoked_count,
                    "usuario": str(stored.user_id),
                },
                context=context,
            )
            self._uow.commit()
            logger.warning(
                "reúso de refresh token detectado; família revogada",
                extra={"family_id": str(stored.family_id), "revogados": revoked_count},
            )
            raise AuthenticationError("sessão encerrada por motivo de segurança")

        if not stored.is_usable(now):
            raise AuthenticationError("token expirado")

        user = self._uow.users.get(stored.user_id)
        if user is None or not user.is_active:
            self._uow.refresh_tokens.revoke_family(stored.family_id, now=now)
            self._uow.commit()
            raise AuthenticationError("token inválido")

        # A família é preservada: o novo token é descendente do mesmo login.
        pair = self._issue_pair(user, context=context, family_id=stored.family_id)
        stored.revoke(now=now)

        self._audit.record(
            action=AuditAction.TOKEN_REFRESH,
            resource_type="user",
            resource_id=user.id,
            details={"familia": str(stored.family_id)},
            context=context.set_actor(user_id=user.id, email=user.email.value, role=user.role),
        )
        self._uow.commit()
        return pair

    # -- Logout ------------------------------------------------------------
    def logout(
        self, *, user_id: uuid.UUID, refresh_token: str | None = None, all_sessions: bool = False
    ) -> int:
        """Revoga a sessão atual ou todas.

        O access token **continua válido até expirar** — é a natureza de um
        token sem estado, e é por isso que o TTL dele é de 15 minutos. Revogar
        o access token exigiria consultar uma lista de bloqueio no banco a cada
        requisição, o que desfaz o motivo de usar JWT. O compromisso está no
        ADR-005.
        """
        now = datetime.now(UTC)
        context = current_context()

        if all_sessions or refresh_token is None:
            revoked = self._uow.refresh_tokens.revoke_all_for_user(user_id, now=now)
        else:
            stored = self._uow.refresh_tokens.get_by_hash(hash_refresh_token(refresh_token))
            if stored is not None and stored.user_id == user_id:
                revoked = self._uow.refresh_tokens.revoke_family(stored.family_id, now=now)
            else:
                revoked = 0

        self._audit.record(
            action=AuditAction.LOGOUT,
            resource_type="user",
            resource_id=user_id,
            details={"sessoes_revogadas": revoked, "todas": all_sessions},
            context=context,
        )
        self._uow.commit()
        return revoked

    # -- Usuários ----------------------------------------------------------
    def create_user(self, *, email: str, password: str, full_name: str, role: Role) -> User:
        parsed = EmailAddress.parse(email)
        validate_password_strength(password, min_length=self._settings.password_min_length)

        if self._uow.users.get_by_email(parsed.value) is not None:
            raise ConflictError("já existe usuário com este e-mail")

        user = User(
            email=parsed,
            password_hash=hash_password(password, rounds=self._settings.bcrypt_rounds),
            role=role,
            full_name=full_name,
        )
        self._uow.users.add(user)
        self._audit.record(
            action=AuditAction.USER_CREATED,
            resource_type="user",
            resource_id=user.id,
            details={"papel": role.value, "email": parsed.masked()},
        )
        self._uow.commit()
        return user

    def change_password(
        self, *, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> None:
        user = self._uow.users.get(user_id)
        if user is None:
            raise NotFoundError("usuário não encontrado")
        if not verify_password(current_password, user.password_hash):
            raise AuthenticationError("senha atual incorreta")
        validate_password_strength(new_password, min_length=self._settings.password_min_length)

        user.change_password(hash_password(new_password, rounds=self._settings.bcrypt_rounds))
        # Trocar a senha encerra todas as sessões. Se a troca foi motivada por
        # suspeita de comprometimento, deixar as sessões antigas vivas
        # esvaziaria o gesto: o invasor continuaria dentro com o refresh token
        # que já tinha.
        revoked = self._uow.refresh_tokens.revoke_all_for_user(user_id, now=datetime.now(UTC))
        self._audit.record(
            action=AuditAction.USER_UPDATED,
            resource_type="user",
            resource_id=user_id,
            details={"campo": "senha", "sessoes_revogadas": revoked},
        )
        self._uow.commit()

    # -- Internos ----------------------------------------------------------
    def _issue_pair(
        self, user: User, *, context: RequestContext, family_id: uuid.UUID
    ) -> TokenPair:
        access = issue_access_token(self._settings, subject=user.id, role=user.role)
        refresh = issue_refresh_token(
            self._settings, subject=user.id, role=user.role, family_id=family_id
        )
        self._uow.refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                token_hash=hash_refresh_token(refresh.token),
                family_id=family_id,
                expires_at=refresh.expires_at,
                created_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )
        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            token_type="Bearer",  # noqa: S106
            expires_in=self._settings.access_token_ttl_minutes * 60,
            role=user.role,
        )

    def _record_login_failure(
        self,
        email: str,
        reason: str,
        context: RequestContext,
        *,
        user: User | None = None,
    ) -> None:
        self._audit.record_isolated(
            action=AuditAction.LOGIN_FAILURE,
            resource_type="user",
            resource_id=user.id if user else None,
            details={
                "motivo": reason,
                # O e-mail vai mascarado: a trilha precisa identificar o alvo
                # de uma força bruta sem virar uma lista de e-mails válidos
                # para quem tiver acesso de leitura à auditoria.
                "email": EmailAddress(email).masked() if "@" in email else "[invalido]",
            },
            context=context,
        )
