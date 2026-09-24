#!/usr/bin/env bash
# Roda localmente exatamente o que o CI roda.
#
# O objetivo é que ninguém descubra no push o que dava para descobrir em 40
# segundos na própria máquina. Quando o portão local e o do CI divergem, o time
# aprende a ignorar o vermelho do CI — que é o pior resultado possível.
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
falhas=0

passo() {
    echo ""
    echo "── $1 ──────────────────────────────────────────"
    shift
    if "$@"; then
        echo "   ok"
    else
        echo "   FALHOU" >&2
        falhas=$((falhas + 1))
    fi
}

passo "lint"              "$PY" -m ruff check .
passo "formatação"        "$PY" -m ruff format --check .
passo "tipos"             "$PY" -m mypy app
passo "testes"            "$PY" -m pytest -q

# `alembic check` precisa de banco. Sem DATABASE_URL acessível ele falharia por
# falta de infraestrutura, não por deriva — e o aviso é mais útil que o erro.
if [ -n "${DATABASE_URL:-}" ]; then
    passo "deriva de schema" "$PY" -m alembic check
else
    echo ""
    echo "── deriva de schema ────────────────────────────"
    echo "   ignorado: defina DATABASE_URL para rodar 'alembic check'"
fi

echo ""
if [ "$falhas" -eq 0 ]; then
    echo "tudo verde."
else
    echo "$falhas passo(s) falharam." >&2
    exit 1
fi
