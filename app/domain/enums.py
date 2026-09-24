"""Enumerações do domínio.

Todas herdam de `str` para serializar como texto legível na API e no banco.
O valor gravado é o texto (`"ACTIVE"`), nunca o índice: renumerar um enum
numérico reescreve o significado de linhas históricas em silêncio.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Perfis do RBAC. A matriz de permissões está em `app/core/permissions.py`."""

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    AUDITOR = "AUDITOR"


class ClientStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ContractStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRING = "EXPIRING"  # dentro da janela de alerta, ainda vigente
    EXPIRED = "EXPIRED"
    RENEWED = "RENEWED"  # substituído por um contrato sucessor
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Estado terminal não aceita edição, renovação nem cancelamento."""
        return self in {ContractStatus.CANCELLED, ContractStatus.RENEWED}


class InstallmentStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class NotificationType(StrEnum):
    CONTRACT_EXPIRING = "CONTRACT_EXPIRING"
    CONTRACT_EXPIRED = "CONTRACT_EXPIRED"
    CONTRACT_RENEWED = "CONTRACT_RENEWED"
    INSTALLMENT_DUE = "INSTALLMENT_DUE"
    INSTALLMENT_OVERDUE = "INSTALLMENT_OVERDUE"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    # Esgotou as tentativas. Separado de FAILED para o operador saber que o
    # sistema não vai tentar de novo sozinho.
    DEAD_LETTER = "DEAD_LETTER"


class AuditAction(StrEnum):
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    LOGOUT = "LOGOUT"
    TOKEN_REFRESH = "TOKEN_REFRESH"  # noqa: S105
    TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"  # noqa: S105
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"

    CLIENT_CREATED = "CLIENT_CREATED"
    CLIENT_UPDATED = "CLIENT_UPDATED"
    CLIENT_DEACTIVATED = "CLIENT_DEACTIVATED"

    CONTRACT_CREATED = "CONTRACT_CREATED"
    CONTRACT_UPDATED = "CONTRACT_UPDATED"
    CONTRACT_RENEWED = "CONTRACT_RENEWED"
    CONTRACT_CANCELLED = "CONTRACT_CANCELLED"
    CONTRACT_EXPIRED = "CONTRACT_EXPIRED"

    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"

    JOB_STARTED = "JOB_STARTED"
    JOB_FINISHED = "JOB_FINISHED"
    JOB_FAILED = "JOB_FAILED"

    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"


class AuditOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class JobStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # outra instância já estava rodando
