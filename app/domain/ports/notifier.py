"""Porta do serviço externo de notificação.

O domínio declara o que precisa ("entregue esta mensagem") e o que espera de
volta. Quem sabe que isso vira um `POST /v1/messages` com `Authorization:
Bearer`, retry e circuit breaker é o adaptador em
`app/infrastructure/gateways/http_notifier.py`.

O valor disso não é acadêmico: trocar o provedor de e-mail por uma fila, ou por
um webhook de Discord, é escrever outra classe que satisfaça este `Protocol`.
Nenhuma linha de `app/services/` muda.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    channel: str
    recipient: str
    subject: str
    body: str
    # Repassado ao provedor para que **ele** também deduplique. Reenvio por
    # retry de rede é normal; mensagem duplicada na caixa do cliente não é.
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class NotificationResult:
    delivered: bool
    provider_message_id: str | None = None
    error: str | None = None
    # Distingue "falhou e pode tentar de novo" (timeout, 503) de "falhou e
    # tentar de novo é inútil" (400, destinatário inválido). Sem isso, o retry
    # queima as três tentativas contra um erro permanente.
    retryable: bool = False
    attempts: int = 1


class NotificationGateway(Protocol):
    def send(self, request: NotificationRequest) -> NotificationResult:
        """Entrega a mensagem. **Não levanta exceção por falha do provedor.**

        Falha de terceiro é resultado esperado, não excepcional: o job da
        madrugada processa um lote, e uma exceção no meio derrubaria as
        notificações seguintes por causa de um destinatário com caixa cheia.
        """
        ...

    def health(self) -> bool:
        """Sondagem rasa, para o endpoint de readiness."""
        ...
