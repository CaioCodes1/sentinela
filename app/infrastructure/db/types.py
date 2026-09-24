"""Tipos de coluna que sabem converter os objetos de valor do domínio.

Sem isto, cada repositório teria que lembrar de fazer `TaxId.parse(row.tax_id)`
na leitura e `client.tax_id.digits` na escrita — e o dia em que alguém esquecer,
entra no banco um CPF sem validação, pela porta dos fundos.

Com o `TypeDecorator`, a conversão acontece na fronteira do SQLAlchemy: o que
entra na coluna é sempre normalizado, o que sai é sempre um objeto de valor já
validado. É o mesmo princípio dos objetos de valor, aplicado uma camada abaixo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, String, TypeDecorator
from sqlalchemy.engine import Dialect

from app.domain.value_objects import EmailAddress, Money, TaxId


class TaxIdType(TypeDecorator[TaxId]):
    """Guarda só os dígitos. A pontuação é apresentação, não dado."""

    impl = String(14)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, TaxId):
            return value.digits
        return TaxId.parse(str(value)).digits

    def process_result_value(self, value: Any, dialect: Dialect) -> TaxId | None:
        return None if value is None else TaxId(value)


class EmailType(TypeDecorator[EmailAddress]):
    impl = String(254)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, EmailAddress):
            return value.value
        return EmailAddress.parse(str(value)).value

    def process_result_value(self, value: Any, dialect: Dialect) -> EmailAddress | None:
        return None if value is None else EmailAddress(value)


class MoneyType(TypeDecorator[Money]):
    """`NUMERIC(14, 2)`, nunca `DOUBLE PRECISION`.

    `float` no PostgreSQL é IEEE 754 binário: `SELECT 0.1::float8 + 0.2::float8`
    devolve `0.30000000000000004`. Somado em doze parcelas e comparado com o
    total do contrato, isso é uma diferença de centavo que ninguém consegue
    explicar para o financeiro.
    """

    impl = Numeric(14, 2)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Money):
            return value.amount
        return Money.parse(value).amount

    def process_result_value(self, value: Any, dialect: Dialect) -> Money | None:
        return None if value is None else Money(Decimal(value).quantize(Decimal("0.01")))


class StrEnumType(TypeDecorator[Any]):
    """Converte uma `StrEnum` do domínio em texto, e de volta.

    Poderia ser dispensável: `StrEnum` já é `str`, então a **escrita** funciona
    sem nada disso. O problema é a **leitura** — sem este decorador o banco
    devolve `str` puro, e `contract.status.is_terminal` estoura com
    `AttributeError` só no caminho que ninguém exercitou em teste.

    Não usamos `ENUM` nativo do PostgreSQL de propósito: acrescentar um valor a
    um tipo enum nativo é DDL, com trava de tabela, e removê-lo é praticamente
    impossível sem recriar o tipo. `VARCHAR` + `CHECK` dá a mesma proteção e a
    migration é uma linha.
    """

    impl = String(40)
    cache_ok = True

    def __init__(self, enum_class: type[Any], length: int = 40) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else self.enum_class(value)
