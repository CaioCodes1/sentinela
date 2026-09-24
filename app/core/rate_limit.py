"""Limitação de taxa.

Algoritmo: **janela deslizante com log de timestamps**. A alternativa barata é
a janela fixa (um contador por minuto, zerado na virada), que tem um defeito
conhecido: com limite de 10/min, um cliente manda 10 às 12:00:59 e mais 10 às
12:01:00 — 20 chamadas em um segundo, dentro do limite pelas contas do
algoritmo. Para proteger login contra força bruta, essa brecha é justamente a
que importa.

Escopo desta implementação: **um processo**. Com várias réplicas atrás de um
balanceador, cada uma conta sozinha e o limite efetivo se multiplica pelo
número de réplicas. Isso é aceitável aqui porque a proteção real contra força
bruta é o **bloqueio de conta**, que vive no banco e é compartilhado; o rate
limit é a primeira barreira, não a última. A porta `RateLimiterPort` existe
para que trocar por Redis seja escrever uma classe — está no ADR-006.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    limit: int


class RateLimiterPort(Protocol):
    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision: ...

    def reset(self, key: str) -> None: ...


class InMemorySlidingWindowLimiter:
    """Janela deslizante em memória, protegida por lock.

    O lock não é zelo excessivo: os endpoints são `def` e rodam no threadpool
    do FastAPI, então duas requisições tocam este dicionário de verdade ao
    mesmo tempo. Sem ele, duas threads leem o mesmo tamanho de fila e as duas
    passam — exatamente na décima chamada, que é a que deveria ser barrada.
    """

    def __init__(self, *, max_keys: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        # Teto de chaves: sem ele, um atacante variando o IP de origem faz o
        # dicionário crescer sem limite — o mecanismo de defesa vira o vetor de
        # exaustão de memória.
        self._max_keys = max_keys

    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.monotonic()  # imune a ajuste de relógio e a horário de verão
        cutoff = now - window_seconds

        with self._lock:
            if key not in self._hits and len(self._hits) >= self._max_keys:
                self._evict_locked(cutoff)

            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_after = max(1, int(hits[0] + window_seconds - now) + 1)
                return RateLimitDecision(
                    allowed=False, remaining=0, retry_after_seconds=retry_after, limit=limit
                )

            hits.append(now)
            return RateLimitDecision(
                allowed=True,
                remaining=limit - len(hits),
                retry_after_seconds=0,
                limit=limit,
            )

    def _evict_locked(self, cutoff: float) -> None:
        """Descarta chaves já vencidas; se ainda estiver cheio, corta as mais antigas."""
        stale = [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]
        for key in stale:
            del self._hits[key]
        if len(self._hits) >= self._max_keys:
            oldest = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
            for key, _ in oldest[: self._max_keys // 10]:
                del self._hits[key]

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class NullRateLimiter:
    """Desligado. Usado nos testes que não são sobre rate limit."""

    def check(self, key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, remaining=limit, retry_after_seconds=0, limit=limit)

    def reset(self, key: str) -> None:  # pragma: no cover - trivial
        return None
