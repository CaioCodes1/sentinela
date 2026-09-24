"""Schemas compartilhados.

Convenção do projeto: **todo schema de entrada recusa campo desconhecido**
(`extra="forbid"`). O padrão do Pydantic é ignorar em silêncio, e ignorar em
silêncio é como um `PATCH` com `{"role": "ADMIN"}` num endpoint que não deveria
aceitar `role` passa despercebido — o campo some, ninguém reclama, e o cliente
acha que funcionou. Recusar transforma a tentativa de *mass assignment* em erro
visível, e ainda pega erro de digitação em nome de campo.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.domain.exceptions import ValidationError as DomainValidationError

T = TypeVar("T")
R = TypeVar("R")


def como_erro_de_campo(conversor: Callable[[Any], R], valor: Any) -> R:
    """Executa um conversor do domínio traduzindo a falha para `ValueError`.

    O Pydantic só transforma em erro de campo o que for `ValueError` ou
    `AssertionError`. A `ValidationError` do domínio é outra classe, então
    ela **atravessa** o validador e chega ao tratador global — que responde
    400, sem dizer qual campo estava errado.

    Duas formas de erro para a mesma classe de problema obrigam o cliente da
    API a tratar dois casos. Com esta tradução, tudo que é entrada malformada
    sai como 422 apontando o campo.
    """
    try:
        return conversor(valor)
    except DomainValidationError as exc:
        raise ValueError(exc.message) from exc


class StrictModel(BaseModel):
    """Base de todo corpo de requisição."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        # Sem `str_to_lower` global: e-mail é normalizado pelo objeto de valor,
        # e nome próprio não deve ser rebaixado.
        validate_assignment=True,
    )


class OutputModel(BaseModel):
    """Base de toda resposta. `from_attributes` lê direto das entidades."""

    model_config = ConfigDict(from_attributes=True)


class Page(OutputModel, Generic[T]):
    items: list[T]
    total: int = Field(description="Total de registros que satisfazem o filtro")
    limit: int
    offset: int

    @property
    def has_next(self) -> bool:  # pragma: no cover - conveniência
        return self.offset + len(self.items) < self.total


class PaginationParams(BaseModel):
    """Limites de paginação validados no schema, não no repositório.

    O teto de 200 também existe no repositório. A duplicação é intencional: a
    API protege a fronteira HTTP, e o repositório protege as chamadas internas
    (job, CLI) que não passam por aqui.
    """

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class ProblemDetail(OutputModel):
    """Corpo de erro no formato RFC 7807. Declarado só para documentar no OpenAPI."""

    type: str
    title: str
    status: int
    code: str
    detail: str
    request_id: str


class MessageResponse(OutputModel):
    message: str
