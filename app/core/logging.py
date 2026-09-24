"""Log estruturado em JSON, com redação de dado pessoal.

Duas decisões que valem explicação.

**Por que JSON.** Log de plataforma corporativa termina num agregador (Loki,
Elastic, CloudWatch). Texto livre obriga a escrever expressão regular para
extrair o id da requisição; JSON permite filtrar por campo. O custo é log menos
bonito no terminal — por isso `LOG_JSON=false` existe para o desenvolvimento.

**Por que redigir.** Log é o vazamento de dado pessoal mais comum e o menos
notado: ninguém trata `logger.info(f"cliente {payload}")` como incidente, mas o
CPF completo fica no agregador por meses, visível para quem tem acesso de
leitura ao log — que é muito mais gente do que tem acesso ao banco. O filtro
abaixo roda em **todo** registro, inclusive nos de biblioteca de terceiro, que
é onde o vazamento aparece sem ninguém ter escrito a linha.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context import current_context

# Padrões de dado pessoal. Aplicados sobre a mensagem já formatada, para pegar
# também o que veio de biblioteca de terceiro.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # CPF com ou sem pontuação
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF_REDIGIDO]"),
    # CNPJ
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), "[CNPJ_REDIGIDO]"),
    # E-mail: preserva a primeira letra e o domínio, que é o suficiente para
    # investigar um caso sem expor a lista de clientes.
    (
        re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"),
        r"\1***@\2",
    ),
    # Token JWT
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        "[JWT_REDIGIDO]",
    ),
    # Cabeçalho Authorization e afins
    (
        re.compile(r"(?i)\b(authorization|bearer|api[_-]?key|password|senha)\b\s*[:=]\s*\S+"),
        r"\1=[REDIGIDO]",
    ),
    # Hash bcrypt, caso alguém logue o registro inteiro do usuário
    (re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"), "[HASH_REDIGIDO]"),
]

# Campos que nunca são serializados, mesmo que cheguem em `extra=`.
_FORBIDDEN_FIELDS = {
    "password",
    "senha",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "secret",
}

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Redige a mensagem antes de qualquer formatador tocá-la."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.getMessage()))
        except Exception:
            record.msg = "[mensagem de log não pôde ser redigida]"
        record.args = ()
        return True


class ContextFilter(logging.Filter):
    """Carimba todo registro com o contexto da requisição corrente."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_context()
        record.request_id = ctx.request_id
        record.actor = ctx.actor_email or "-"
        record.client_ip = ctx.ip_address or "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "actor": getattr(record, "actor", "-"),
            "client_ip": getattr(record, "client_ip", "-"),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            if key.lower() in _FORBIDDEN_FIELDS:
                payload[key] = "[REDIGIDO]"
                continue
            payload[key] = _safe(value)

        if record.exc_info:
            # `formatException` pode trazer valores de variável local no
            # traceback; passa pelo mesmo filtro que o resto.
            payload["exception"] = redact(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def _safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return redact(value) if isinstance(value, str) else value
    return redact(str(value))


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    # A ordem importa: redigir primeiro, carimbar depois. Invertido, o filtro de
    # contexto veria a mensagem ainda com o dado bruto.
    handler.addFilter(RedactionFilter())
    handler.addFilter(ContextFilter())
    root.addHandler(handler)

    # O uvicorn instala os próprios handlers e duplicaria cada linha, além de
    # escapar da redação. Aqui eles passam a delegar para a raiz.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True

    # O SQLAlchemy em nível INFO imprime toda consulta com os parâmetros
    # ligados — ou seja, CPF e e-mail em texto puro no log de produção.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
