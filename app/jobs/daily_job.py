"""A função que o agendador chama.

Ela monta as próprias dependências em vez de receber injeção do FastAPI, porque
roda fora do ciclo de vida de uma requisição. É deliberadamente magra: toda a
lógica está em `AutomationService`, que é testável sem agendador e sem HTTP.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.infrastructure.db.session import get_session_factory, init_engine
from app.infrastructure.db.uow import SqlAlchemyUnitOfWork
from app.infrastructure.gateways.http_notifier import HttpNotificationGateway
from app.services.automation_service import AutomationService

logger = get_logger(__name__)


def run_daily_check(
    *, reference: date | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Executa a verificação diária e devolve as estatísticas."""
    settings = settings or get_settings()
    init_engine(settings)  # idempotente: reaproveita o engine do processo

    gateway = HttpNotificationGateway(settings)
    try:
        with SqlAlchemyUnitOfWork(get_session_factory()) as uow:
            service = AutomationService(uow, gateway, settings)
            return service.run_daily(reference=reference)
    except Exception:
        # O agendador engole exceção do job por padrão e a execução some. Este
        # log é o que garante que uma falha apareça em algum lugar mesmo se a
        # gravação em `job_runs` também tiver falhado.
        logger.exception("execução agendada falhou")
        raise
    finally:
        gateway.close()
