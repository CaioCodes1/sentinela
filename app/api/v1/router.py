"""Agregador das rotas da versão 1.

O prefixo `/api/v1` está aqui, e não espalhado em cada roteador: quando existir
uma v2, o caminho é montar outro agregador com os roteadores que mudaram e
reaproveitar os que não mudaram. Versão embutida em cada arquivo obrigaria a
tocar em todos.
"""

from fastapi import APIRouter

from app.api.v1 import audit, auth, clients, contracts, operations

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(clients.router)
api_router.include_router(contracts.router)
api_router.include_router(operations.router)
api_router.include_router(audit.router)
