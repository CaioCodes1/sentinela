"""Erros do domínio.

Nenhum deles conhece HTTP. A tradução para status code acontece uma única vez,
em `app/core/errors.py`. Isso é o que permite chamar as regras a partir do job
agendado e do CLI sem arrastar FastAPI para dentro do domínio.

Todos carregam um `code` estável, pensado para o cliente da API programar em
cima dele em vez de comparar a mensagem — que muda quando alguém corrige uma
vírgula.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    code = "validation_error"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    """Choque com o estado atual: documento repetido, número de contrato em uso."""

    code = "conflict"


class BusinessRuleError(DomainError):
    """A operação é válida na forma, mas proibida pela regra de negócio."""

    code = "business_rule_violation"


class ConcurrencyError(DomainError):
    """Outra transação alterou o mesmo registro (trava otimista)."""

    code = "concurrent_modification"


class AuthenticationError(DomainError):
    code = "authentication_failed"


class AuthorizationError(DomainError):
    code = "not_authorized"


class AccountLockedError(AuthenticationError):
    code = "account_locked"


class RateLimitError(DomainError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class ExternalServiceError(DomainError):
    """Falha ao falar com um serviço de terceiro. Nunca vaza a resposta crua dele."""

    code = "external_service_unavailable"
