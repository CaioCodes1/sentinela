"""Sondas de saúde.

Três endpoints, com propósitos diferentes — confundi-los é o erro comum:

- `/health/live` (**liveness**): o processo está de pé? Não toca em banco nem em
  rede. Se ele consultasse o banco, uma queda do PostgreSQL faria o orquestrador
  **reiniciar a aplicação** — que é o remédio errado: reiniciar não conserta o
  banco, e o efeito é uma nuvem de contêineres em laço de reinício enquanto o
  banco tenta se recuperar.
- `/health/ready` (**readiness**): dá para receber tráfego? Aqui sim as
  dependências são verificadas. Falha tira a instância do balanceador sem
  matá-la.
- `/health`: resumo legível para humano.

Nenhum deles exige autenticação, e por isso nenhum devolve versão de
biblioteca, host do banco ou string de conexão.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import SettingsDep, get_container
from app.infrastructure.db.session import check_database

router = APIRouter(tags=["Saúde"])


@router.get("/health/live", summary="Liveness: o processo responde")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness: dependências acessíveis")
def ready(response: Response, settings: SettingsDep) -> dict[str, Any]:
    container = get_container()
    database_ok = check_database(container.engine)
    notifier_ok = container.notification_gateway.health()

    # O provedor de notificação **não** entra na decisão de prontidão. Ele é
    # assíncrono por natureza: a fila absorve a indisponibilidade e reenvia
    # depois. Tirar a API do ar porque o serviço de e-mail caiu seria deixar
    # de atender contrato e cliente por causa de um aviso que pode esperar.
    healthy = database_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if healthy else "degraded",
        "checks": {
            "database": "ok" if database_ok else "falha",
            "notification_provider": "ok" if notifier_ok else "indisponível",
        },
    }


@router.get("/health", summary="Resumo de saúde")
def health(settings: SettingsDep) -> dict[str, str]:
    return {
        "status": "ok",
        "application": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
    }
