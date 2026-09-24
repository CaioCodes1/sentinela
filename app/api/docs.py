"""Página da documentação, servida por nós para podermos assinar o script.

O `/docs` do FastAPI é HTML com um `<script>` embutido que inicializa o Swagger
UI. Uma CSP com `script-src` sem `'unsafe-inline'` bloqueia esse script, e a
página abre **em branco, sem erro nenhum na resposta** — o motivo só aparece no
console do navegador. Nenhum teste de API pega isso, porque teste de API não
renderiza página.

Em vez de liberar todo script embutido, a aplicação serve a página ela mesma,
calcula o hash SHA-256 do script que acabou de gerar e declara esse hash na
CSP. A política continua recusando qualquer outro script na página, e o hash
acompanha sozinho uma mudança de versão do FastAPI.
"""

from __future__ import annotations

import base64
import hashlib
import re

from fastapi import APIRouter
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

router = APIRouter()

_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)

_html_em_cache: str | None = None
_hash_em_cache: str | None = None


def _renderiza(titulo: str = "Sentinela") -> str:
    """O HTML do Swagger UI, exatamente como será entregue ao navegador."""
    resposta = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{titulo} - Swagger UI",
        # A documentação não usa fluxo OAuth2 com redirecionamento: a
        # autenticação é Bearer, colada na própria tela do `Authorize`.
        oauth2_redirect_url=None,
    )
    # `Response.body` é declarado como `bytes | memoryview`; o `bytes()` cobre
    # os dois e satisfaz o mypy sem `cast`.
    return bytes(resposta.body).decode("utf-8")


def _carrega() -> tuple[str, str]:
    """Gera a página uma vez e guarda junto o hash do script embutido."""
    global _html_em_cache, _hash_em_cache
    if _html_em_cache is None or _hash_em_cache is None:
        html = _renderiza()
        # O hash cobre o conteúdo do script **sem** as tags, que é o que a CSP
        # compara. Incluir as tags produz um hash que nunca casa, e o sintoma é
        # idêntico ao de não ter hash nenhum.
        partes = [
            base64.b64encode(hashlib.sha256(corpo.encode("utf-8")).digest()).decode()
            for corpo in _SCRIPT.findall(html)
            if corpo.strip()
        ]
        _html_em_cache = html
        _hash_em_cache = " ".join(f"'sha256-{p}'" for p in partes)
    return _html_em_cache, _hash_em_cache


def hash_do_swagger() -> str:
    """Os hashes prontos para entrar no `script-src`, separados por espaço."""
    return _carrega()[1]


@router.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    html, _ = _carrega()
    return HTMLResponse(html)
