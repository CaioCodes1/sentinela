"""Objetos de valor: tipos que carregam a própria regra de validade.

O ganho não é estético. Enquanto o CPF for `str`, qualquer string entra no
banco e a validação depende de alguém lembrar de chamá-la. Sendo `TaxId`, o
único jeito de existir uma instância é tendo passado pelo dígito verificador.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.domain.exceptions import ValidationError

_NON_DIGITS = re.compile(r"\D")
# Sem quantificador aninhado e sem alternância ambígua: esta expressão não tem
# retrocesso catastrófico. Regex de e-mail "esperta" é vetor de ReDoS.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){1,4}$")
_CONTROL_CHARS = {"Cc", "Cf"}


def strip_control_characters(value: str) -> str:
    """Remove caracteres de controle e invisíveis de formatação.

    Cobre o truque do bidirectional override (`U+202E`), que faz um nome
    aparecer invertido na tela sem mudar o byte gravado, e o zero-width space,
    usado para burlar comparação de strings em lista de bloqueio.
    """
    return "".join(ch for ch in value if unicodedata.category(ch) not in _CONTROL_CHARS)


def normalize_text(value: str, *, max_length: int, field: str) -> str:
    """Normaliza (NFC), tira controle, colapsa espaço e confere o tamanho."""
    cleaned = strip_control_characters(unicodedata.normalize("NFC", value))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise ValidationError(f"{field} não pode ser vazio")
    if len(cleaned) > max_length:
        raise ValidationError(f"{field} excede {max_length} caracteres")
    return cleaned


@dataclass(frozen=True, slots=True)
class TaxId:
    """CPF ou CNPJ, guardado só com dígitos e validado pelo dígito verificador."""

    digits: str

    def __post_init__(self) -> None:
        if len(self.digits) == 11:
            if not _is_valid_cpf(self.digits):
                raise ValidationError("CPF inválido")
        elif len(self.digits) == 14:
            if not _is_valid_cnpj(self.digits):
                raise ValidationError("CNPJ inválido")
        else:
            raise ValidationError("documento precisa ter 11 (CPF) ou 14 (CNPJ) dígitos")

    @classmethod
    def parse(cls, raw: str) -> TaxId:
        """Aceita `123.456.789-09` ou `12345678909` e guarda só os dígitos.

        Guardar formatado permitiria o mesmo documento entrar duas vezes com
        pontuação diferente e escapar da restrição de unicidade.
        """
        if not isinstance(raw, str):
            raise ValidationError("documento precisa ser texto")
        return cls(_NON_DIGITS.sub("", raw))

    @property
    def is_company(self) -> bool:
        return len(self.digits) == 14

    def masked(self) -> str:
        """Forma segura para log e mensagem de erro: `***.***.789-09`."""
        if self.is_company:
            return f"**.***.***/{self.digits[8:12]}-{self.digits[12:]}"
        return f"***.***.{self.digits[6:9]}-{self.digits[9:]}"

    def formatted(self) -> str:
        d = self.digits
        if self.is_company:
            return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.digits


def _check_digit(digits: str, weights: list[int]) -> int:
    total = sum(int(d) * w for d, w in zip(digits, weights, strict=True))
    remainder = total % 11
    return 0 if remainder < 2 else 11 - remainder


def _is_valid_cpf(digits: str) -> bool:
    if not digits.isdigit() or len(set(digits)) == 1:
        return False
    first = _check_digit(digits[:9], list(range(10, 1, -1)))
    second = _check_digit(digits[:10], list(range(11, 1, -1)))
    return digits[9] == str(first) and digits[10] == str(second)


def _is_valid_cnpj(digits: str) -> bool:
    if not digits.isdigit() or len(set(digits)) == 1:
        return False
    first = _check_digit(digits[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = _check_digit(digits[:13], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return digits[12] == str(first) and digits[13] == str(second)


@dataclass(frozen=True, slots=True)
class EmailAddress:
    value: str

    def __post_init__(self) -> None:
        if not _EMAIL_RE.match(self.value):
            raise ValidationError("e-mail inválido")
        if len(self.value) > 254:
            raise ValidationError("e-mail excede 254 caracteres")

    @classmethod
    def parse(cls, raw: str) -> EmailAddress:
        """Normaliza para minúsculas.

        Sem isso, `Ana@x.com` e `ana@x.com` viram duas contas e a restrição de
        unicidade não impede o cadastro duplicado.
        """
        if not isinstance(raw, str):
            raise ValidationError("e-mail precisa ser texto")
        return cls(strip_control_characters(raw).strip().lower())

    def masked(self) -> str:
        """`ana.silva@empresa.com` -> `a*******a@empresa.com`."""
        local, _, domain = self.value.partition("@")
        if len(local) <= 2:
            return f"{local[0]}*@{domain}"
        return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True, slots=True)
class Money:
    """Valor monetário em `Decimal`, sempre com duas casas.

    `float` está proibido no domínio financeiro: `0.1 + 0.2 != 0.3` em binário,
    e o erro aparece como centavo perdido depois de doze parcelas somadas.
    """

    amount: Decimal

    def __post_init__(self) -> None:
        if self.amount != self.amount.quantize(Decimal("0.01")):
            raise ValidationError("valor monetário só aceita duas casas decimais")

    @classmethod
    def parse(cls, raw: Decimal | int | str) -> Money:
        try:
            value = Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError("valor monetário inválido") from exc
        return cls(value)

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount)

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money.parse(self.amount * Decimal(str(factor)))

    def is_positive(self) -> bool:
        return self.amount > 0

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.amount:.2f}"
