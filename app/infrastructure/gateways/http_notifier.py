"""Adaptador HTTP para o serviço externo de notificações.

Este arquivo existe porque **integração com terceiro é onde sistemas
corporativos quebram**, e quase nunca por causa do caminho feliz. O que trata o
caminho infeliz:

- **timeout separado por fase** (conexão, escrita, leitura);
- **retry com espera exponencial e jitter**, só para o que é retentável;
- **distinção entre falha temporária e permanente** — `400 destinatário
  inválido` não melhora na terceira tentativa;
- **disjuntor (circuit breaker)**, para parar de martelar um serviço caído;
- **chave de idempotência**, para que o retry não vire mensagem duplicada;
- **log de erro sem vazar corpo de resposta de terceiro**.

Sem o disjuntor, um provedor fora do ar transforma um lote de 500 notificações
em 1.500 tentativas de 5 segundos cada: duas horas de job travado, enquanto o
resto da automação não roda.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.core.config import Settings
from app.core.context import current_context
from app.core.logging import get_logger
from app.domain.ports.notifier import NotificationRequest, NotificationResult

logger = get_logger(__name__)

# Códigos que valem uma nova tentativa. 429 e 5xx são o provedor dizendo
# "agora não"; 408 é timeout do lado dele.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class CircuitState(StrEnum):
    CLOSED = "CLOSED"  # operando normalmente
    OPEN = "OPEN"  # provedor considerado fora do ar, recusa rápida
    HALF_OPEN = "HALF_OPEN"  # deixa passar uma sonda


@dataclass
class _CircuitBreaker:
    """Disjuntor de três estados.

    O estado HALF_OPEN é o detalhe que costuma faltar. Sem ele, o disjuntor ou
    fica aberto por tempo fixo (e continua recusando depois que o provedor já
    voltou), ou reabre direto para o tráfego inteiro (e derruba de novo um
    serviço que estava só se recuperando). HALF_OPEN deixa passar **uma** sonda:
    se ela der certo, fecha; se falhar, reabre o relógio.
    """

    failure_threshold: int
    reset_after_seconds: float
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def allows_request(self) -> bool:
        with self._lock:
            if self.state is CircuitState.CLOSED:
                return True
            if self.state is CircuitState.OPEN:
                if time.monotonic() - self.opened_at >= self.reset_after_seconds:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("disjuntor em meia-abertura: liberando sonda")
                    return True
                return False
            # HALF_OPEN: uma sonda por vez.
            return True

    def record_success(self) -> None:
        with self._lock:
            if self.state is not CircuitState.CLOSED:
                logger.info("disjuntor fechado: provedor respondeu")
            self.state = CircuitState.CLOSED
            self.failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state is CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
                if self.state is not CircuitState.OPEN:
                    logger.warning(
                        "disjuntor aberto para o serviço de notificação",
                        extra={"falhas_consecutivas": self.failures},
                    )
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()


class HttpNotificationGateway:
    """Cliente do provedor externo."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._max_attempts = settings.notifier_max_attempts
        self._backoff_base = settings.notifier_backoff_base_seconds
        self._breaker = _CircuitBreaker(
            failure_threshold=settings.notifier_circuit_failure_threshold,
            reset_after_seconds=settings.notifier_circuit_reset_seconds,
        )
        # Um cliente por instância, reaproveitando conexões. Criar um
        # `httpx.Client` por chamada refaz o aperto de mão TLS toda vez — em
        # lote de centenas de notificações isso domina o tempo do job.
        self._client = client or httpx.Client(
            base_url=settings.notifier_base_url,
            timeout=httpx.Timeout(
                # Timeout total não basta: uma conexão que fica pendurada no
                # aperto de mão consome o orçamento inteiro antes de qualquer
                # byte trafegar. Separado por fase, o problema fica localizável.
                connect=min(3.0, settings.notifier_timeout_seconds),
                read=settings.notifier_timeout_seconds,
                write=settings.notifier_timeout_seconds,
                pool=settings.notifier_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,  # redirecionamento levaria a chave de API para outro host
        )

    # -- API pública -------------------------------------------------------
    def send(self, request: NotificationRequest) -> NotificationResult:
        if not self._breaker.allows_request():
            # Recusa imediata, sem gastar timeout. O chamador registra como
            # falha retentável e a notificação volta para a fila.
            return NotificationResult(
                delivered=False,
                error="disjuntor aberto: provedor indisponível",
                retryable=True,
                attempts=0,
            )

        last_error = "falha desconhecida"
        retryable = True

        for attempt in range(1, self._max_attempts + 1):
            outcome = self._attempt(request, attempt)
            if outcome.delivered:
                self._breaker.record_success()
                return NotificationResult(
                    delivered=True,
                    provider_message_id=outcome.provider_message_id,
                    attempts=attempt,
                )

            last_error, retryable = outcome.error or last_error, outcome.retryable
            if not retryable:
                # Erro permanente não conta para o disjuntor: o provedor está
                # de pé e respondendo — o problema é o nosso dado. Contá-lo
                # abriria o disjuntor por causa de um lote de e-mails inválidos
                # e bloquearia as notificações boas.
                return NotificationResult(
                    delivered=False, error=last_error, retryable=False, attempts=attempt
                )

            self._breaker.record_failure()
            if attempt < self._max_attempts:
                time.sleep(self._backoff_delay(attempt))

        return NotificationResult(
            delivered=False, error=last_error, retryable=True, attempts=self._max_attempts
        )

    def health(self) -> bool:
        try:
            response = self._client.get("/health", timeout=2.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()

    # -- Internos ----------------------------------------------------------
    def _backoff_delay(self, attempt: int) -> float:
        """Espera exponencial **com jitter**.

        O jitter não é refinamento: sem ele, 500 notificações que falharam no
        mesmo segundo repetem juntas exatamente 0,5 s depois, e de novo 1 s
        depois. O provedor que estava se recuperando leva a mesma rajada em
        sincronia e cai outra vez — o "problema do rebanho trovejante". A
        aleatoriedade espalha as tentativas no tempo.

        `random` comum basta aqui: isto é escalonamento, não segredo.
        """
        exponential = self._backoff_base * (2 ** (attempt - 1))
        return min(exponential + random.uniform(0, self._backoff_base), 30.0)  # noqa: S311

    def _attempt(self, request: NotificationRequest, attempt: int) -> NotificationResult:
        try:
            response = self._client.post(
                "/v1/messages",
                json={
                    "channel": request.channel,
                    "to": request.recipient,
                    "subject": request.subject,
                    "body": request.body,
                },
                headers={
                    "Authorization": f"Bearer {self._settings.notifier_api_key.get_secret_value()}",
                    # O provedor usa esta chave para descartar o duplicado que
                    # nasce de um retry após timeout — o caso em que a
                    # mensagem foi entregue mas a resposta se perdeu.
                    "Idempotency-Key": request.idempotency_key,
                    # Propaga o id da requisição: quando o provedor manda o log
                    # dele numa investigação, dá para casar com o nosso.
                    "X-Request-Id": current_context().request_id,
                },
            )
        except httpx.TimeoutException:
            logger.warning(
                "timeout no serviço de notificação",
                extra={"tentativa": attempt, "destino": _mask(request.recipient)},
            )
            return NotificationResult(delivered=False, error="timeout", retryable=True)
        except httpx.HTTPError as exc:
            # Falha de conexão, DNS, TLS. A classe da exceção é informação
            # suficiente no log; a mensagem pode conter a URL com credencial.
            logger.warning(
                "falha de conexão com o serviço de notificação",
                extra={"tentativa": attempt, "erro": type(exc).__name__},
            )
            return NotificationResult(
                delivered=False, error=f"falha de conexão: {type(exc).__name__}", retryable=True
            )

        if response.status_code in (200, 201, 202):
            return NotificationResult(
                delivered=True, provider_message_id=_extract_id(response), attempts=attempt
            )

        retryable = response.status_code in RETRYABLE_STATUS
        logger.warning(
            "serviço de notificação recusou a mensagem",
            extra={
                "tentativa": attempt,
                "status_code": response.status_code,
                "retentavel": retryable,
                # O corpo da resposta **não** entra no log: vem de fora, pode
                # ter dado pessoal ecoado e pode ter tamanho arbitrário.
            },
        )
        return NotificationResult(
            delivered=False,
            error=f"provedor respondeu {response.status_code}",
            retryable=retryable,
        )


def _extract_id(response: httpx.Response) -> str | None:
    """Lê o id da mensagem tolerando resposta fora do contrato.

    Resposta de terceiro é entrada não confiável como qualquer outra: pode vir
    sem JSON, com JSON de outro formato, ou com um `id` de 10 KB. Explodir aqui
    marcaria como falha um envio que **deu certo**, e o retry mandaria de novo.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("id") or payload.get("message_id")
    return str(value)[:120] if value is not None else None


def _mask(recipient: str) -> str:
    if "@" not in recipient:
        return f"{recipient[:3]}***"
    local, _, domain = recipient.partition("@")
    return f"{local[:1]}***@{domain}"
