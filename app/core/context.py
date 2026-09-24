"""Contexto da requisição, propagado por `contextvars`.

O problema que isto resolve: a linha de auditoria precisa de IP, user-agent,
id da requisição e quem é o autor — e quem grava a auditoria é o serviço, que
não deveria conhecer `Request` do FastAPI. A alternativa seria passar cinco
parâmetros por toda a cadeia de chamadas até o repositório.

`contextvars` é o mecanismo certo para isso em Python: ao contrário de uma
variável global, ele é isolado por tarefa assíncrona **e** por thread. Como os
endpoints deste projeto são `def` (rodam no threadpool), esse isolamento não é
detalhe: sem ele, duas requisições simultâneas gravariam auditoria com o IP
trocado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.domain.enums import Role


@dataclass(slots=True)
class RequestContext:
    """Dados da requisição corrente.

    **Mutável de propósito, e este é o ponto mais sutil do arquivo.**

    Os endpoints deste projeto são `def`, então o FastAPI executa cada
    dependência e cada handler no threadpool, via `anyio.to_thread.run_sync`.
    Essa função **copia** o contexto atual para cada chamada. A consequência:
    um `ContextVar.set()` feito dentro de uma dependência morre ali — a
    dependência seguinte recebe outra cópia, sem a alteração.

    Foi exatamente o que aconteceu na primeira versão, com `RequestContext`
    imutável: `get_current_user` resolvia o usuário e chamava
    `set_context(ctx.with_actor(...))`, e mesmo assim toda linha de auditoria
    saía com `actor_email` nulo. Nenhum erro, nenhum aviso — só uma trilha
    inútil, que é o pior defeito possível numa trilha de auditoria.

    A correção é a cópia do contexto compartilhar o **mesmo objeto**: copiar um
    `Context` copia o mapa `var -> valor`, e o valor continua sendo a mesma
    instância. Mutar o objeto é visível em todas as cópias; trocar a referência
    do `ContextVar`, não.
    """

    request_id: str
    ip_address: str | None = None
    user_agent: str | None = None
    actor_user_id: uuid.UUID | None = None
    actor_email: str | None = None
    actor_role: Role | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def set_actor(self, *, user_id: uuid.UUID, email: str, role: Role) -> RequestContext:
        """Registra quem é o autor, **no lugar**. Devolve `self` por conveniência."""
        self.actor_user_id = user_id
        self.actor_email = email
        self.actor_role = role
        return self


_EMPTY = RequestContext(request_id="-")
_current: ContextVar[RequestContext] = ContextVar("sentinela_request_context", default=_EMPTY)


def current_context() -> RequestContext:
    return _current.get()


def set_context(context: RequestContext) -> None:
    _current.set(context)


def new_request_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def use_context(context: RequestContext) -> Iterator[RequestContext]:
    """Instala um contexto e restaura o anterior na saída.

    O `reset` no `finally` é o que impede o contexto de vazar entre execuções
    quando a mesma thread do pool atende a próxima requisição — e é também o
    que faz o job agendado não herdar o ator da última chamada HTTP.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def system_context(job_name: str) -> RequestContext:
    """Contexto do processo automático.

    A automação também é auditada, e com autor explícito: `system:<job>`. Deixar
    `actor` nulo faria a trilha ficar cheia de linhas sem responsável, e ninguém
    saberia distinguir "o sistema cancelou" de "faltou registrar quem cancelou".
    """
    return RequestContext(
        request_id=f"job-{uuid.uuid4().hex[:12]}",
        ip_address=None,
        user_agent=f"sentinela-scheduler/{job_name}",
        actor_email=f"system:{job_name}",
    )
