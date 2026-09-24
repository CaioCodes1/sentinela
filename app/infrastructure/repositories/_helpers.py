"""Auxiliares compartilhados pelos repositórios."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

T = TypeVar("T")

# Teto de página. Sem ele, `?limit=1000000` é um pedido legítimo pela API que
# carrega a tabela inteira na memória do processo — negação de serviço com uma
# única requisição bem-comportada.
MAX_PAGE_SIZE = 200


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE_SIZE))


def clamp_offset(offset: int) -> int:
    return max(0, offset)


def escape_like(term: str) -> str:
    r"""Neutraliza os curingas do `LIKE` dentro do termo de busca.

    Sem isto, buscar por `%` casa com **todos** os registros, e `_` casa com
    qualquer caractere. Não é injeção de SQL — os parâmetros continuam ligados
    corretamente — mas é o mesmo tipo de descuido: tratar entrada do usuário
    como se fosse sintaxe. O `\` como escape é declarado na própria cláusula.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def paginate(
    session: Session, statement: Select[Any], *, limit: int, offset: int
) -> tuple[Sequence[Any], int]:
    """Devolve `(linhas da página, total sem paginação)`.

    O total sai de uma subconsulta sobre o mesmo `Select` já filtrado, e não de
    um `len()` sobre o resultado — que exigiria trazer tudo para a memória só
    para contar, desfazendo o propósito da paginação.
    """
    total_stmt = select(func.count()).select_from(statement.order_by(None).subquery())
    total = session.execute(total_stmt).scalar_one()
    rows = session.execute(statement.limit(clamp_limit(limit)).offset(clamp_offset(offset)))
    return list(rows.scalars().all()), int(total)
