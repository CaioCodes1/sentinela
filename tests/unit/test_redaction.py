"""Redação de dado pessoal no log e na auditoria.

Log é o vazamento de dado pessoal mais comum e o menos notado: ninguém trata
`logger.info(f"cliente {payload}")` como incidente, mas o CPF completo fica no
agregador por meses — visível para muito mais gente do que tem acesso ao banco.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC

import pytest

from app.core.logging import JsonFormatter, RedactionFilter, redact
from app.services.audit_service import MAX_DETAIL_KEYS, MAX_DETAIL_LEN, _sanitize


class TestRedacaoDeTexto:
    @pytest.mark.parametrize(
        "texto",
        [
            "cliente com CPF 529.982.247-25 cadastrado",
            "cliente com CPF 52998224725 cadastrado",
        ],
    )
    def test_cpf_com_e_sem_pontuacao(self, texto: str) -> None:
        saida = redact(texto)
        assert "[CPF_REDIGIDO]" in saida
        assert "529" not in saida

    def test_cnpj(self) -> None:
        saida = redact("empresa 12.345.678/0001-95")
        assert "[CNPJ_REDIGIDO]" in saida
        assert "0001" not in saida

    def test_email_preserva_o_dominio(self) -> None:
        """O domínio fica: serve para investigar sem expor a lista de clientes."""
        saida = redact("enviado para ana.silva@empresa.com.br")
        assert "ana.silva" not in saida
        assert "@empresa.com.br" in saida

    def test_jwt(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"
        assert "[JWT_REDIGIDO]" in redact(f"Bearer {token}")

    def test_hash_bcrypt(self) -> None:
        hashed = "$2b$12$" + "a" * 53
        assert "[HASH_REDIGIDO]" in redact(f"hash do usuário: {hashed}")

    @pytest.mark.parametrize(
        "linha",
        [
            "Authorization: Bearer abc123",
            "api_key=super-secreto",
            "password=MinhaSenha123",
            "senha: qualquercoisa",
        ],
    )
    def test_credenciais_em_formato_chave_valor(self, linha: str) -> None:
        assert "[REDIGIDO]" in redact(linha)

    def test_texto_sem_dado_pessoal_passa_intacto(self) -> None:
        original = "contrato CT-2026-001 renovado por 12 meses"
        assert redact(original) == original


class TestFiltroDeLog:
    def test_filtro_redige_antes_do_formatador(self) -> None:
        registro = logging.LogRecord(
            name="teste",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="cadastrando CPF 529.982.247-25",
            args=(),
            exc_info=None,
        )
        RedactionFilter().filter(registro)
        assert "529.982.247-25" not in registro.getMessage()

    def test_campo_proibido_em_extra_nao_e_serializado(self) -> None:
        """Mesmo passando de propósito, a senha não chega ao log."""
        registro = logging.LogRecord(
            name="teste",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="login",
            args=(),
            exc_info=None,
        )
        registro.password = "MinhaSenhaSecreta"  # type: ignore[attr-defined]
        registro.refresh_token = "eyJ..."  # type: ignore[attr-defined]

        saida = json.loads(JsonFormatter().format(registro))
        assert saida["password"] == "[REDIGIDO]"
        assert saida["refresh_token"] == "[REDIGIDO]"

    def test_formato_json_tem_os_campos_de_correlacao(self) -> None:
        registro = logging.LogRecord(
            name="teste",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="ok",
            args=(),
            exc_info=None,
        )
        saida = json.loads(JsonFormatter().format(registro))
        for campo in ("timestamp", "level", "logger", "message", "request_id"):
            assert campo in saida

    def test_traceback_tambem_passa_pela_redacao(self) -> None:
        """O traceback pode trazer valores de variável local — inclusive a senha
        que estava em memória no momento da exceção."""
        try:
            senha = "529.982.247-25"
            raise ValueError(f"falhou processando {senha}")
        except ValueError:
            import sys

            registro = logging.LogRecord(
                name="teste",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="erro",
                args=(),
                exc_info=sys.exc_info(),
            )
        saida = json.loads(JsonFormatter().format(registro))
        assert "529.982.247-25" not in saida["exception"]


class TestSanitizacaoDaAuditoria:
    def test_chave_proibida_e_redigida(self) -> None:
        limpo = _sanitize({"usuario": "ana", "password": "segredo", "token": "abc"})
        assert limpo["usuario"] == "ana"
        assert limpo["password"] == "[REDIGIDO]"
        assert limpo["token"] == "[REDIGIDO]"

    def test_redacao_alcanca_estrutura_aninhada(self) -> None:
        limpo = _sanitize({"alteracoes": {"credenciais": {"senha": "x"}}})
        assert limpo["alteracoes"]["credenciais"]["senha"] == "[REDIGIDO]"

    def test_profundidade_excessiva_e_podada(self) -> None:
        """JSON aninhado sem fim já derrubou serializador em produção."""
        fundo: dict = {"n": "fim"}
        for _ in range(10):
            fundo = {"nivel": fundo}
        limpo = _sanitize(fundo)
        assert "_truncado" in json.dumps(limpo, ensure_ascii=False)

    def test_texto_longo_e_cortado(self) -> None:
        """Um `details` de 2 MB por linha transforma a auditoria no maior
        objeto do banco em poucas semanas."""
        limpo = _sanitize({"observacao": "x" * 5000})
        assert len(limpo["observacao"]) == MAX_DETAIL_LEN

    def test_excesso_de_chaves_e_truncado(self) -> None:
        limpo = _sanitize({f"campo{i}": i for i in range(MAX_DETAIL_KEYS + 20)})
        assert len(limpo) <= MAX_DETAIL_KEYS + 1
        assert "_truncado" in limpo

    def test_resultado_e_serializavel_em_json(self) -> None:
        """O `details` vai para uma coluna JSONB: o que não serializa, quebra
        a gravação da auditoria — e derruba junto a operação auditada."""
        import uuid
        from datetime import datetime
        from decimal import Decimal

        limpo = _sanitize(
            {
                "id": uuid.uuid4(),
                "quando": datetime.now(UTC),
                "valor": Decimal("10.50"),
                "lista": [1, 2, {"a": "b"}],
            }
        )
        json.dumps(limpo, ensure_ascii=False)  # não pode levantar
