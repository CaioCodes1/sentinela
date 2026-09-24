"""Objetos de valor: documento, e-mail e dinheiro."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.exceptions import ValidationError
from app.domain.value_objects import (
    EmailAddress,
    Money,
    TaxId,
    normalize_text,
    strip_control_characters,
)


class TestTaxId:
    @pytest.mark.parametrize(
        "entrada",
        ["529.982.247-25", "52998224725", " 529 982 247 25 ", "529982247-25"],
    )
    def test_aceita_qualquer_pontuacao_e_guarda_so_digitos(self, entrada: str) -> None:
        """Normalizar é o que faz a restrição de unicidade funcionar.

        Guardando formatado, `529.982.247-25` e `52998224725` seriam dois
        clientes distintos para o índice único.
        """
        assert TaxId.parse(entrada).digits == "52998224725"

    @pytest.mark.parametrize(
        "invalido",
        [
            "529.982.247-26",  # dígito verificador errado
            "111.111.111-11",  # todos iguais
            "123",  # curto
            "5299822472512345",  # longo
            "abc.def.ghi-jk",  # sem dígitos
        ],
    )
    def test_recusa_documento_invalido(self, invalido: str) -> None:
        with pytest.raises(ValidationError):
            TaxId.parse(invalido)

    def test_cnpj_valido(self) -> None:
        documento = TaxId.parse("12.345.678/0001-95")
        assert documento.is_company is True
        assert documento.formatted() == "12.345.678/0001-95"

    def test_mascara_esconde_o_inicio(self) -> None:
        """A máscara é o que vai para log e auditoria."""
        mascarado = TaxId.parse("529.982.247-25").masked()
        assert mascarado == "***.***.247-25"
        assert "529" not in mascarado

    def test_e_imutavel(self) -> None:
        documento = TaxId.parse("52998224725")
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            documento.digits = "00000000000"  # type: ignore[misc]


class TestEmailAddress:
    def test_normaliza_para_minusculas(self) -> None:
        """Sem isso, `Ana@x.com` e `ana@x.com` viram duas contas."""
        assert EmailAddress.parse("  Ana.Silva@Empresa.COM  ").value == "ana.silva@empresa.com"

    @pytest.mark.parametrize(
        "invalido",
        ["sem-arroba", "@sem-local.com", "duplo@@x.com", "a@b", "espaço no@meio.com"],
    )
    def test_recusa_endereco_invalido(self, invalido: str) -> None:
        with pytest.raises(ValidationError):
            EmailAddress.parse(invalido)

    def test_mascara_preserva_dominio(self) -> None:
        mascarado = EmailAddress.parse("ana.silva@empresa.com.br").masked()
        assert mascarado.endswith("@empresa.com.br")
        assert "silva" not in mascarado

    def test_regex_nao_tem_retrocesso_catastrofico(self) -> None:
        """Entrada patológica não pode travar a validação (ReDoS).

        Uma expressão com quantificador aninhado — do tipo `(a+)+@` — faz o
        motor de regex explorar exponencialmente as combinações e trava o
        processo com uma única requisição. Este teste mede: se demorar, é
        porque a expressão tem o defeito.
        """
        import time

        patologico = "a" * 2000 + "!@" + "b" * 2000
        inicio = time.perf_counter()
        with pytest.raises(ValidationError):
            EmailAddress.parse(patologico)
        assert time.perf_counter() - inicio < 0.5


class TestMoney:
    def test_usa_decimal_e_nao_float(self) -> None:
        """A soma que o binário erra.

        `0.1 + 0.2` em `float` dá 0.30000000000000004. Em `Decimal`, dá 0.30 —
        e é por isso que valor monetário nunca usa ponto flutuante.
        """
        total = Money.parse("0.10") + Money.parse("0.20")
        assert total.amount == Decimal("0.30")
        assert total.amount != Decimal(str(0.1 + 0.2))

    def test_arredonda_para_duas_casas_pela_metade_acima(self) -> None:
        assert Money.parse("10.005").amount == Decimal("10.01")
        assert Money.parse("10.004").amount == Decimal("10.00")

    def test_multiplicacao_mantem_duas_casas(self) -> None:
        reajustado = Money.parse("1333.33") * Decimal("1.075")
        assert reajustado.amount == Decimal("1433.33")

    def test_doze_parcelas_nao_perdem_centavo(self) -> None:
        soma = Money.parse("0.00")
        for _ in range(12):
            soma = soma + Money.parse("83.33")
        assert soma.amount == Decimal("999.96")

    @pytest.mark.parametrize("invalido", ["abc", "", "R$ 10,00", None])
    def test_recusa_valor_invalido(self, invalido) -> None:
        with pytest.raises(ValidationError):
            Money.parse(invalido)


class TestSanitizacaoDeTexto:
    def test_remove_override_bidirecional(self) -> None:
        """`U+202E` inverte a exibição sem mudar o byte gravado.

        É o truque clássico para fazer um nome aparecer diferente na tela do
        operador e no relatório. Como a comparação de strings não vê diferença,
        só a remoção resolve.
        """
        limpo = strip_control_characters("Contrato‮gnp.exe")
        assert "‮" not in limpo

    def test_remove_espaco_de_largura_zero(self) -> None:
        assert strip_control_characters("ad​min") == "admin"

    def test_colapsa_espaco_em_branco(self) -> None:
        assert normalize_text("  Aurora   Serviços  ", max_length=50, field="nome") == (
            "Aurora Serviços"
        )

    def test_recusa_texto_vazio_apos_limpeza(self) -> None:
        with pytest.raises(ValidationError, match="vazio"):
            normalize_text("   ​  ", max_length=50, field="nome")

    def test_recusa_texto_longo(self) -> None:
        with pytest.raises(ValidationError, match="excede"):
            normalize_text("x" * 200, max_length=100, field="nome")
