"""A página do `/docs` precisa poder executar o próprio script.

Este teste existe por um defeito real, encontrado em 24/09/2026 **abrindo a
página no navegador**, não rodando a suíte: a CSP liberava o CDN em
`script-src` mas não o script que o FastAPI embute na página, e o Swagger UI
abria em branco. Resposta 200, corpo correto, nenhum erro no log — o motivo só
aparecia no console do navegador.

O teste não renderiza nada. Ele compara duas coisas que precisam concordar: o
hash que a CSP declara e o hash do script que a página realmente entrega. Se
alguém mudar a forma de gerar a página, ou a versão do FastAPI mudar o HTML,
isto falha aqui em vez de falhar na tela de quem abrir a documentação.
"""

from __future__ import annotations

import base64
import hashlib
import re

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)


def _cliente() -> TestClient:
    settings = Settings(
        app_env="development",
        docs_enabled=True,
        jwt_secret="segredo-de-teste-com-tamanho-mais-que-suficiente-01234",
        database_url="postgresql+psycopg://ninguem:ninguem@localhost:1/vazio",
        notifier_api_key="chave-de-teste",
        bcrypt_rounds=4,
    )
    # Sem `with`: o `lifespan` abriria conexão com um banco que não existe, e
    # esta página não toca no banco.
    return TestClient(create_app(settings))


def _hash(corpo: str) -> str:
    return base64.b64encode(hashlib.sha256(corpo.encode("utf-8")).digest()).decode()


class TestDocumentacaoInterativa:
    def test_a_pagina_abre(self) -> None:
        resposta = _cliente().get("/docs")
        assert resposta.status_code == 200
        assert "swagger-ui" in resposta.text.lower()

    def test_a_csp_assina_o_script_embutido(self) -> None:
        resposta = _cliente().get("/docs")
        csp = resposta.headers["content-security-policy"]

        embutidos = [c for c in _SCRIPT.findall(resposta.text) if c.strip()]
        assert embutidos, "a página deixou de ter script embutido; reveja a CSP"

        for corpo in embutidos:
            esperado = f"'sha256-{_hash(corpo)}'"
            assert esperado in csp, (
                "o script embutido na página não está assinado na CSP: o "
                "navegador vai recusá-lo e a documentação abre em branco, sem "
                "erro nenhum na resposta"
            )

    def test_a_politica_nao_libera_script_embutido_em_geral(self) -> None:
        csp = _cliente().get("/docs").headers["content-security-policy"]
        diretiva = next(p for p in csp.split(";") if p.strip().startswith("script-src"))
        assert "unsafe-inline" not in diretiva, (
            "liberar todo script embutido resolveria o sintoma e valeria para "
            "qualquer script injetado na página; o hash vale só para o nosso"
        )
