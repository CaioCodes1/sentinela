"""Schemas de auditoria."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.api.schemas.common import OutputModel
from app.domain.enums import AuditAction, AuditOutcome, Role


class AuditLogResponse(OutputModel):
    id: uuid.UUID
    occurred_at: datetime
    action: AuditAction
    outcome: AuditOutcome
    resource_type: str
    resource_id: str | None
    actor_user_id: uuid.UUID | None
    actor_email: str | None = Field(
        description="E-mail do autor no momento do ato; preservado se o usuário mudar"
    )
    actor_role: Role | None
    ip_address: str | None
    request_id: str | None
    details: dict


class AuditFilters(OutputModel):
    """Filtros aceitos pela consulta de auditoria, documentados no OpenAPI."""

    actor_user_id: uuid.UUID | None = None
    action: AuditAction | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
