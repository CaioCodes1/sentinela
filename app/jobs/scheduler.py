"""Agendador da automação diária.

APScheduler dentro do próprio processo da API, e não um contêiner de cron
separado. A justificativa e as alternativas descartadas estão no ADR-007; o
resumo é que a exclusão mútua fica garantida pela trava consultiva no
PostgreSQL, então rodar em toda réplica é seguro e dispensa uma peça a mais na
infraestrutura.

Três ajustes que evitam problemas conhecidos de agendador:

- **`coalesce=True`**: se o processo ficar suspenso e três disparos vencerem,
  executa **uma** vez, não três.
- **`misfire_grace_time`**: execução atrasada além da janela é descartada em
  vez de rodar fora de hora — um job das 3h disparando às 11h manda e-mail em
  horário comercial.
- **`max_instances=1`**: impede sobreposição dentro do mesmo processo, que é o
  que a trava do banco não cobre (ela é por transação, entre processos).
"""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings
from app.core.logging import get_logger
from app.jobs.daily_job import run_daily_check

logger = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler(settings: Settings) -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_check,
        trigger=CronTrigger(hour=settings.daily_job_hour, minute=settings.daily_job_minute),
        id="verificacao-diaria-contratos",
        name="Verificação diária de contratos",
        coalesce=True,
        misfire_grace_time=3600,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "agendador iniciado",
        extra={"hora_utc": f"{settings.daily_job_hour:02d}:{settings.daily_job_minute:02d}"},
    )
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        # `wait=False`: não segura o encerramento esperando um job que pode
        # levar minutos. A execução interrompida não deixa estado inconsistente
        # porque cada passo é transacional e o job é idempotente — na próxima
        # execução ele refaz o que ficou pendente e nada em dobro.
        _scheduler.shutdown(wait=False)
        logger.info("agendador parado")
    _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
