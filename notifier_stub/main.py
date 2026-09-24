"""Serviço externo de notificações — simulado.

Isto **não** faz parte da aplicação. É um terceiro fictício, com o mesmo formato
de um provedor transacional real (SendGrid, Twilio, Amazon SES): autenticação
por Bearer, `Idempotency-Key`, `202 Accepted` com id de mensagem.

Existe por um motivo prático: o tratamento de timeout, retry, disjuntor e fila
morta em `app/infrastructure/gateways/http_notifier.py` só é demonstrável se o
outro lado puder **falhar de verdade**. Um duplo de teste em memória prova a
lógica; um serviço HTTP que demora, cai e devolve 500 prova o caminho completo,
com rede no meio.

Modos de falha, controlados por variável de ambiente:

    STUB_FAILURE_RATE=0.3    30% das chamadas respondem 503 (retentável)
    STUB_TIMEOUT_RATE=0.1    10% demoram mais que o timeout do cliente
    STUB_LATENCY_MS=120      latência base

E por cabeçalho, para teste determinístico — que é o que se usa em demonstração:

    X-Stub-Behavior: fail | timeout | reject | ok

Rode com:

    uvicorn notifier_stub.main:app --port 9090
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

app = FastAPI(
    title="Provedor de Notificações (simulado)",
    version="1.0.0",
    description="Terceiro fictício usado para exercitar retry, timeout e disjuntor.",
)

FAILURE_RATE = float(os.getenv("STUB_FAILURE_RATE", "0.25"))
TIMEOUT_RATE = float(os.getenv("STUB_TIMEOUT_RATE", "0.10"))
LATENCY_MS = int(os.getenv("STUB_LATENCY_MS", "80"))
API_KEY = os.getenv("STUB_API_KEY", "chave-do-stub-local")

# Memória de chaves de idempotência já vistas. Um provedor real guarda isso por
# 24h; aqui vive enquanto o processo viver. É o que permite demonstrar que o
# retry após timeout **não** duplica a mensagem na caixa do destinatário.
_vistas: dict[str, str] = {}
_entregues: list[dict[str, Any]] = []


class MessageIn(BaseModel):
    channel: str = Field(max_length=20)
    to: str = Field(max_length=254)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=10_000)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    payload: MessageIn,
    request: Request,
    authorization: str = Header(default=""),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    if authorization != f"Bearer {API_KEY}":
        # 401 é **permanente**: o cliente não deve tentar de novo com a mesma
        # chave errada. É o caso que prova que o retry sabe distinguir
        # "tente de novo" de "não adianta".
        raise HTTPException(status_code=401, detail="chave de API inválida")

    comportamento = request.headers.get("X-Stub-Behavior", "").lower()

    if comportamento == "timeout" or (not comportamento and random.random() < TIMEOUT_RATE):
        # Dorme muito mais que qualquer timeout razoável do cliente. Quem
        # desiste é o chamador — que é exatamente o que se quer exercitar.
        await asyncio.sleep(30)
        return {"id": "nunca-chega"}

    if comportamento == "fail" or (not comportamento and random.random() < FAILURE_RATE):
        raise HTTPException(status_code=503, detail="provedor temporariamente indisponível")

    if comportamento == "reject":
        raise HTTPException(status_code=400, detail="destinatário rejeitado pelo provedor")

    await asyncio.sleep(LATENCY_MS / 1000)

    if idempotency_key and idempotency_key in _vistas:
        # Mesma resposta da primeira vez, sem entregar de novo.
        return {
            "id": _vistas[idempotency_key],
            "status": "duplicate_ignored",
            "accepted_at": datetime.now(UTC).isoformat(),
        }

    message_id = f"msg_{uuid.uuid4().hex[:16]}"
    if idempotency_key:
        _vistas[idempotency_key] = message_id

    _entregues.append(
        {
            "id": message_id,
            "channel": payload.channel,
            "to": payload.to,
            "subject": payload.subject,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    return {
        "id": message_id,
        "status": "accepted",
        "accepted_at": datetime.now(UTC).isoformat(),
    }


@app.get("/v1/messages")
async def list_delivered(limit: int = 50) -> dict[str, Any]:
    """Caixa de saída do provedor — útil para conferir o que a automação mandou."""
    return {"total": len(_entregues), "items": _entregues[-limit:]}


@app.post("/v1/reset")
async def reset() -> dict[str, str]:
    _vistas.clear()
    _entregues.clear()
    return {"status": "limpo"}
