"""Regras de negócio puras — o núcleo do domínio, sem banco."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.enums import ContractStatus
from app.domain.exceptions import BusinessRuleError, ValidationError
from app.domain.rules import (
    add_months,
    days_until_expiration,
    ensure_can_be_modified,
    is_expired,
    matched_alert_threshold,
    next_status_for,
    plan_installments,
    plan_renewal,
    resolve_due_date,
)
from app.domain.value_objects import Money


class TestAritmeticaDeMeses:
    @pytest.mark.parametrize(
        ("origem", "meses", "esperado"),
        [
            # O caso que a soma ingênua de 30 dias erra.
            (date(2026, 1, 31), 1, date(2026, 2, 28)),
            (date(2028, 1, 31), 1, date(2028, 2, 29)),  # bissexto
            (date(2026, 1, 31), 2, date(2026, 3, 31)),
            (date(2026, 3, 31), 1, date(2026, 4, 30)),
            (date(2026, 12, 15), 1, date(2027, 1, 15)),  # vira o ano
            (date(2026, 6, 10), 12, date(2027, 6, 10)),
            (date(2026, 6, 10), 0, date(2026, 6, 10)),
        ],
    )
    def test_preserva_o_dia_quando_ele_existe(self, origem, meses, esperado) -> None:
        assert add_months(origem, meses) == esperado

    def test_nao_acumula_deriva_ao_repetir(self) -> None:
        """Somar 1 mês doze vezes não pode chegar antes de somar 12 de uma vez.

        Com a aritmética de "30 dias", cada passo perde um dia e o erro se
        acumula: doze renovações mensais terminariam cinco dias antes do que
        deveriam.
        """
        cursor = date(2026, 1, 15)
        for _ in range(12):
            cursor = add_months(cursor, 1)
        assert cursor == add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)


class TestDiaDeVencimento:
    def test_dia_31_vira_o_ultimo_dia_do_mes_curto(self) -> None:
        assert resolve_due_date(2026, 2, 31) == date(2026, 2, 28)
        assert resolve_due_date(2028, 2, 31) == date(2028, 2, 29)
        assert resolve_due_date(2026, 4, 31) == date(2026, 4, 30)

    def test_dia_valido_permanece(self) -> None:
        assert resolve_due_date(2026, 3, 10) == date(2026, 3, 10)

    @pytest.mark.parametrize("dia", [0, 32, -1, 99])
    def test_dia_fora_do_intervalo_e_recusado(self, dia: int) -> None:
        with pytest.raises(ValidationError):
            resolve_due_date(2026, 3, dia)


class TestVencimento:
    def test_no_dia_do_termino_o_contrato_ainda_vale(self) -> None:
        """Último dia de vigência **não** é vencido.

        Tratar `end_date == hoje` como vencido encerraria a cobertura do
        cliente um dia antes da hora.
        """
        hoje = date(2026, 9, 17)
        assert is_expired(date(2026, 9, 17), hoje) is False
        assert is_expired(date(2026, 9, 16), hoje) is True

    def test_dias_restantes_fica_negativo_apos_vencer(self) -> None:
        assert days_until_expiration(date(2026, 9, 20), date(2026, 9, 17)) == 3
        assert days_until_expiration(date(2026, 9, 10), date(2026, 9, 17)) == -7


class TestLimiarDeAlerta:
    LIMIARES = [30, 15, 7, 1]

    def test_dispara_apenas_no_limiar_exato(self) -> None:
        hoje = date(2026, 9, 17)
        assert matched_alert_threshold(date(2026, 10, 17), hoje, self.LIMIARES) == 30
        assert matched_alert_threshold(date(2026, 9, 24), hoje, self.LIMIARES) == 7

    def test_nao_dispara_entre_limiares(self) -> None:
        """Este é o teste que impede o cliente de receber 30 e-mails.

        Com `<=` em vez de igualdade, um contrato a 20 dias do vencimento
        casaria com o limiar de 30 **todo dia** até vencer.
        """
        hoje = date(2026, 9, 17)
        assert matched_alert_threshold(date(2026, 10, 7), hoje, self.LIMIARES) is None
        assert matched_alert_threshold(date(2026, 9, 30), hoje, self.LIMIARES) is None

    def test_contrato_ja_vencido_nao_gera_alerta_de_proximidade(self) -> None:
        assert matched_alert_threshold(date(2026, 9, 10), date(2026, 9, 17), self.LIMIARES) is None


class TestTransicaoDeStatus:
    def test_ativo_vira_a_vencer_dentro_da_janela(self) -> None:
        novo = next_status_for(ContractStatus.ACTIVE, date(2026, 10, 1), date(2026, 9, 17), 30)
        assert novo is ContractStatus.EXPIRING

    def test_ativo_permanece_ativo_fora_da_janela(self) -> None:
        novo = next_status_for(ContractStatus.ACTIVE, date(2027, 10, 1), date(2026, 9, 17), 30)
        assert novo is ContractStatus.ACTIVE

    @pytest.mark.parametrize("terminal", [ContractStatus.CANCELLED, ContractStatus.RENEWED])
    def test_estado_terminal_nunca_e_ressuscitado(self, terminal) -> None:
        """A automação não pode reabrir contrato cancelado ou já renovado.

        Sem esta guarda, o job da madrugada marcaria como EXPIRING um contrato
        que o cliente cancelou — e mandaria aviso de renovação para ele.
        """
        assert next_status_for(terminal, date(2026, 10, 1), date(2026, 9, 17), 30) is terminal

    def test_operacao_em_contrato_terminal_e_recusada(self) -> None:
        with pytest.raises(BusinessRuleError, match="CANCELLED"):
            ensure_can_be_modified(ContractStatus.CANCELLED, "editar")


class TestRenovacao:
    def test_sucessor_comeca_no_dia_seguinte_sem_sobreposicao(self) -> None:
        """Um dia coberto por dois contratos é um dia cobrado duas vezes."""
        plano = plan_renewal(
            current_end_date=date(2026, 12, 31),
            term_months=12,
            monthly_amount=Money.parse("1000.00"),
        )
        assert plano.start_date == date(2027, 1, 1)
        assert plano.end_date == date(2027, 12, 31)
        assert (plano.start_date - date(2026, 12, 31)).days == 1

    def test_reajuste_percentual_aplicado_com_duas_casas(self) -> None:
        plano = plan_renewal(
            current_end_date=date(2026, 12, 31),
            term_months=12,
            monthly_amount=Money.parse("1333.33"),
            adjustment_percent=Decimal("7.5"),
        )
        # 1333.33 * 1.075 = 1433.32975 -> arredonda para 1433.33
        assert plano.monthly_amount == Money.parse("1433.33")
        assert plano.monthly_amount.amount.as_tuple().exponent == -2

    def test_renovacao_de_contrato_que_termina_em_fevereiro(self) -> None:
        plano = plan_renewal(
            current_end_date=date(2026, 2, 28),
            term_months=12,
            monthly_amount=Money.parse("500.00"),
        )
        assert plano.start_date == date(2026, 3, 1)
        assert plano.end_date == date(2027, 2, 28)

    @pytest.mark.parametrize("percentual", [Decimal("-60"), Decimal("150")])
    def test_reajuste_absurdo_e_recusado(self, percentual: Decimal) -> None:
        with pytest.raises(ValidationError, match="reajuste"):
            plan_renewal(
                current_end_date=date(2026, 12, 31),
                term_months=12,
                monthly_amount=Money.parse("1000.00"),
                adjustment_percent=percentual,
            )

    def test_prazo_absurdo_e_recusado(self) -> None:
        with pytest.raises(ValidationError, match="prazo"):
            plan_renewal(
                current_end_date=date(2026, 12, 31),
                term_months=1200,
                monthly_amount=Money.parse("1000.00"),
            )


class TestGeracaoDeParcelas:
    def test_uma_parcela_por_competencia_coberta(self) -> None:
        parcelas = plan_installments(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            due_day=10,
            monthly_amount=Money.parse("250.00"),
        )
        assert len(parcelas) == 12
        assert parcelas[0].competence == "2026-01"
        assert parcelas[-1].competence == "2026-12"
        assert all(p.amount == Money.parse("250.00") for p in parcelas)

    def test_vencimento_fora_da_vigencia_nao_gera_parcela(self) -> None:
        """Contrato de 01/03 a 15/05 com vencimento dia 20 tem duas parcelas.

        Março e abril entram; maio não, porque 20/05 é depois do fim da
        vigência. Sem esta checagem, o cliente receberia cobrança de um mês que
        o contrato não cobre.
        """
        parcelas = plan_installments(
            start_date=date(2026, 3, 1),
            end_date=date(2026, 5, 15),
            due_day=20,
            monthly_amount=Money.parse("100.00"),
        )
        assert [p.competence for p in parcelas] == ["2026-03", "2026-04"]

    def test_primeiro_mes_parcial_nao_gera_parcela_retroativa(self) -> None:
        """Contrato assinado dia 15 com vencimento dia 10 não cobra o dia 10.

        O vencimento de janeiro já passou quando o contrato começou.
        """
        parcelas = plan_installments(
            start_date=date(2026, 1, 15),
            end_date=date(2026, 6, 30),
            due_day=10,
            monthly_amount=Money.parse("100.00"),
        )
        assert parcelas[0].competence == "2026-02"

    def test_vencimento_dia_31_ajusta_por_mes(self) -> None:
        parcelas = plan_installments(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 4, 30),
            due_day=31,
            monthly_amount=Money.parse("100.00"),
        )
        vencimentos = [p.due_date for p in parcelas]
        assert vencimentos == [
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
            date(2026, 4, 30),
        ]

    def test_periodo_invertido_e_recusado(self) -> None:
        with pytest.raises(ValidationError, match="posterior"):
            plan_installments(
                start_date=date(2026, 6, 1),
                end_date=date(2026, 1, 1),
                due_day=10,
                monthly_amount=Money.parse("100.00"),
            )
