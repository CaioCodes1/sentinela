"""Regras de negócio puras: sem banco, sem HTTP, sem relógio implícito.

Toda função aqui recebe a data de referência como parâmetro em vez de chamar
`date.today()` por dentro. É o que torna o comportamento de virada de mês
testável sem congelar o relógio do processo inteiro — e o que evita o clássico
"o teste passa até o dia 29 de fevereiro".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.domain.enums import ContractStatus, NotificationType
from app.domain.exceptions import BusinessRuleError, ValidationError
from app.domain.value_objects import Money

# Teto de segurança: um contrato de 1200 meses provavelmente é erro de digitação
# de quem quis 12, e gerar 1200 parcelas trava o job da madrugada.
MAX_RENEWAL_TERM_MONTHS = 120
MAX_CONTRACT_YEARS = 30


def add_months(reference: date, months: int) -> date:
    """Soma meses preservando o dia quando ele existe no mês de destino.

    `31/01 + 1 mês` é `28/02` (ou 29 em ano bissexto), não `03/03`. A aritmética
    ingênua de somar 30 dias erra a data de renovação de contratos assinados no
    fim do mês — e erra sempre para o mesmo lado, acumulando deriva a cada
    renovação.
    """
    total = reference.month - 1 + months
    year = reference.year + total // 12
    month = total % 12 + 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def resolve_due_date(year: int, month: int, due_day: int) -> date:
    """Traduz "vence todo dia 31" para uma data real no mês pedido.

    Fevereiro não tem 31. Guardar o dia contratado e resolver na hora de gerar
    a parcela preserva a intenção — "no último dia do mês" continua valendo em
    todos os meses — enquanto gravar 28 fixo quebraria março em diante.
    """
    if not 1 <= due_day <= 31:
        raise ValidationError("dia de vencimento precisa estar entre 1 e 31")
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def validate_contract_period(start_date: date, end_date: date) -> None:
    if end_date <= start_date:
        raise ValidationError("a data de término precisa ser posterior à de início")
    if end_date.year - start_date.year > MAX_CONTRACT_YEARS:
        raise ValidationError(f"vigência acima de {MAX_CONTRACT_YEARS} anos não é aceita")


def validate_renewal_term(term_months: int) -> None:
    if term_months < 1:
        raise ValidationError("o prazo de renovação precisa ser de ao menos 1 mês")
    if term_months > MAX_RENEWAL_TERM_MONTHS:
        raise ValidationError(
            f"o prazo de renovação não pode passar de {MAX_RENEWAL_TERM_MONTHS} meses"
        )


def validate_monthly_amount(amount: Money) -> None:
    if not amount.is_positive():
        raise ValidationError("o valor da mensalidade precisa ser maior que zero")
    if amount.amount > Decimal("99999999.99"):
        raise ValidationError("valor da mensalidade acima do limite suportado")


def days_until_expiration(end_date: date, reference: date) -> int:
    """Negativo quando o contrato já venceu."""
    return (end_date - reference).days


def is_expired(end_date: date, reference: date) -> bool:
    """Vencido é a partir do dia *seguinte* ao término.

    No próprio dia do término o contrato ainda vale — é o último dia de
    vigência. Tratar `end_date == hoje` como vencido cancela a cobertura de
    alguém um dia antes da hora, e é o tipo de erro que só aparece em produção,
    num único cliente, uma vez por mês.
    """
    return end_date < reference


def matched_alert_threshold(end_date: date, reference: date, thresholds: list[int]) -> int | None:
    """Devolve o limiar exato atingido hoje, ou `None`.

    A comparação é por igualdade, não por `<=`. Com `<=`, um contrato que vence
    em 20 dias dispararia o alerta de 30 todo santo dia até vencer — trinta
    e-mails para o mesmo cliente. Por igualdade, cada limiar dispara uma vez.
    """
    remaining = days_until_expiration(end_date, reference)
    return remaining if remaining in set(thresholds) else None


def next_status_for(
    current: ContractStatus, end_date: date, reference: date, alert_window_days: int
) -> ContractStatus:
    """Estado que o contrato deveria ter na data de referência.

    Só transiciona a partir de ACTIVE/EXPIRING. Contrato cancelado ou já
    substituído por renovação é terminal: a automação não pode ressuscitá-lo.
    """
    if current.is_terminal or current is ContractStatus.EXPIRED:
        return current
    if is_expired(end_date, reference):
        return ContractStatus.EXPIRED
    if days_until_expiration(end_date, reference) <= alert_window_days:
        return ContractStatus.EXPIRING
    return ContractStatus.ACTIVE


def ensure_can_be_modified(status: ContractStatus, operation: str) -> None:
    if status.is_terminal:
        raise BusinessRuleError(
            f"não é possível {operation} um contrato com status {status.value}",
            details={"status": status.value},
        )


def ensure_can_be_renewed(status: ContractStatus) -> None:
    ensure_can_be_modified(status, "renovar")
    if status is ContractStatus.EXPIRED:
        # Renovar contrato vencido é decisão comercial legítima (renovação
        # retroativa), então é permitido — o que não se permite é renovar o que
        # foi cancelado ou já renovado, que geraria dois sucessores.
        return


@dataclass(frozen=True, slots=True)
class RenewalPlan:
    """Resultado do cálculo de renovação, antes de qualquer escrita."""

    start_date: date
    end_date: date
    monthly_amount: Money
    term_months: int


def plan_renewal(
    *,
    current_end_date: date,
    term_months: int,
    monthly_amount: Money,
    adjustment_percent: Decimal | None = None,
) -> RenewalPlan:
    """Calcula o período e o valor do contrato sucessor.

    O novo período começa **no dia seguinte** ao término do anterior. Começar no
    mesmo dia criaria um dia coberto por dois contratos — e duas cobranças, que
    é exatamente o defeito que ninguém percebe até o cliente ligar.
    """
    validate_renewal_term(term_months)

    new_start = date.fromordinal(current_end_date.toordinal() + 1)
    new_end = add_months(new_start, term_months)
    # `add_months` devolve o mesmo dia do mês; recuar um dia fecha a vigência
    # no dia anterior, deixando os períodos encostados sem sobreposição.
    new_end = date.fromordinal(new_end.toordinal() - 1)

    new_amount = monthly_amount
    if adjustment_percent is not None:
        if adjustment_percent < Decimal("-50") or adjustment_percent > Decimal("100"):
            raise ValidationError("reajuste precisa estar entre -50% e 100%")
        factor = (Decimal("100") + adjustment_percent) / Decimal("100")
        new_amount = monthly_amount * factor

    validate_monthly_amount(new_amount)
    validate_contract_period(new_start, new_end)
    return RenewalPlan(
        start_date=new_start,
        end_date=new_end,
        monthly_amount=new_amount,
        term_months=term_months,
    )


@dataclass(frozen=True, slots=True)
class PlannedInstallment:
    competence: str  # "AAAA-MM"
    due_date: date
    amount: Money


def plan_installments(
    *, start_date: date, end_date: date, due_day: int, monthly_amount: Money
) -> list[PlannedInstallment]:
    """Gera uma parcela por competência coberta pela vigência.

    A parcela só existe se o vencimento cair **dentro** do período do contrato.
    Sem essa checagem, um contrato de 01/03 a 15/05 com vencimento dia 20 geraria
    uma parcela em 20/05, depois do fim da vigência.
    """
    validate_monthly_amount(monthly_amount)
    validate_contract_period(start_date, end_date)

    planned: list[PlannedInstallment] = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        due = resolve_due_date(cursor.year, cursor.month, due_day)
        if start_date <= due <= end_date:
            planned.append(
                PlannedInstallment(
                    competence=f"{cursor.year:04d}-{cursor.month:02d}",
                    due_date=due,
                    amount=monthly_amount,
                )
            )
        cursor = add_months(cursor, 1)
    return planned


def notification_dedupe_key(
    *, contract_id: str, notification_type: NotificationType, marker: str
) -> str:
    """Chave natural de uma notificação, usada como restrição de unicidade.

    É o que sustenta a idempotência do job: rodar duas vezes no mesmo dia tenta
    inserir a mesma chave, e o banco recusa a segunda. A garantia fica no
    índice único, não na memória do processo — dois processos concorrentes não
    compartilham memória, mas compartilham o banco.
    """
    return f"{notification_type.value}:{contract_id}:{marker}"
