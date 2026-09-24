"""Serviço de auditoria.

Regra que define o desenho: **a auditoria é gravada na mesma transação do ato
auditado**. Ou os dois acontecem, ou nenhum. Gravar auditoria em transação
separada parece mais robusto ("assim o log nunca se perde") e produz o pior dos
mundos — trilha afirmando que um contrato foi cancelado quando o cancelamento
falhou no commit.

O caso de falha de autenticação é a exceção deliberada, e está explicada em
`record_isolated`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.context import RequestContext, current_context
from app.core.logging import get_logger
from app.domain.entities import AuditLog
from app.domain.enums import AuditAction, AuditOutcome
from app.domain.ports.uow import UnitOfWork

logger = get_logger(__name__)


class AuditService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def record(
        self,
        *,
        action: AuditAction,
        resource_type: str,
        resource_id: str | uuid.UUID | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        details: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> AuditLog:
        """Registra na transação corrente. **Não** comita."""
        ctx = context or current_context()
        entry = AuditLog(
            action=action,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            actor_user_id=ctx.actor_user_id,
            actor_email=ctx.actor_email,
            actor_role=ctx.actor_role,
            ip_address=ctx.ip_address,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            details=_sanitize(details or {}),
        )
        self._uow.audit.add(entry)
        logger.info(
            "auditoria: %s",
            action.value,
            extra={
                "audit_action": action.value,
                "audit_outcome": outcome.value,
                "resource_type": resource_type,
                "resource_id": str(resource_id) if resource_id else None,
            },
        )
        return entry

    def record_isolated(
        self,
        *,
        action: AuditAction,
        resource_type: str,
        resource_id: str | uuid.UUID | None = None,
        outcome: AuditOutcome = AuditOutcome.FAILURE,
        details: dict[str, Any] | None = None,
        context: RequestContext | None = None,
    ) -> None:
        """Registra **e comita na hora**.

        Existe para um caso específico: tentativa de login malsucedida. Ali o
        fluxo termina levantando `AuthenticationError`, e a exceção provoca o
        rollback da transação — que levaria a linha de auditoria junto. O
        resultado seria uma trilha sem nenhum registro de força bruta,
        exatamente o evento que mais importa registrar.

        Usar isto em operação normal seria um erro: veja a nota no topo.
        """
        self.record(
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
            context=context,
        )
        self._uow.commit()


# Chaves que nunca podem ir para o `details` da auditoria. A trilha é lida por
# perfis que não têm acesso ao dado original — é registro de *quem fez o quê*,
# não uma segunda cópia da base.
_FORBIDDEN_KEYS = {
    "password",
    "senha",
    "password_hash",
    "new_password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "authorization",
}

MAX_DETAIL_LEN = 500
MAX_DETAIL_KEYS = 40


def _sanitize(details: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Poda o `details` antes de ele virar JSONB.

    Três limites, cada um por um motivo:

    - **chaves proibidas**, para a senha não entrar na trilha por descuido de
      quem passou o corpo da requisição inteiro;
    - **profundidade**, porque JSON aninhado sem fim já derrubou serializador;
    - **tamanho**, porque um `details` de 2 MB por linha transforma a tabela de
      auditoria no maior objeto do banco em algumas semanas.
    """
    if depth > 3:
        return {"_truncado": "profundidade máxima atingida"}

    clean: dict[str, Any] = {}
    for index, (key, value) in enumerate(details.items()):
        if index >= MAX_DETAIL_KEYS:
            clean["_truncado"] = f"{len(details) - MAX_DETAIL_KEYS} chaves omitidas"
            break
        if key.lower() in _FORBIDDEN_KEYS:
            clean[key] = "[REDIGIDO]"
            continue
        clean[key] = _sanitize_value(value, depth)
    return clean


def _sanitize_value(value: Any, depth: int) -> Any:
    if isinstance(value, dict):
        return _sanitize(value, depth + 1)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:MAX_DETAIL_LEN]
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)[:MAX_DETAIL_LEN]
