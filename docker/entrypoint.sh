#!/usr/bin/env sh
# Ponto de entrada do contêiner da API.
#
# Três passos antes de servir tráfego: esperar o banco, aplicar as migrations e
# garantir o administrador inicial. Só então o processo principal assume.
#
# `set -e`: qualquer passo que falhe derruba o contêiner em vez de subir uma API
# apontando para um banco sem schema — que responderia 500 em toda requisição e
# pareceria um defeito da aplicação.
set -e

esperar_banco() {
    echo "[entrypoint] aguardando o banco de dados..."
    tentativa=0
    until python - <<'PY'
import sys
from sqlalchemy import create_engine, text
from app.core.config import get_settings

try:
    engine = create_engine(get_settings().database_url.get_secret_value())
    with engine.connect() as conexao:
        conexao.execute(text("SELECT 1"))
except Exception:
    sys.exit(1)
PY
    do
        tentativa=$((tentativa + 1))
        if [ "$tentativa" -ge 30 ]; then
            echo "[entrypoint] banco não respondeu depois de 30 tentativas" >&2
            exit 1
        fi
        sleep 2
    done
    echo "[entrypoint] banco disponível"
}

esperar_banco

# As migrations rodam aqui, e não num contêiner separado, porque a pilha de
# demonstração tem uma réplica só. Com várias réplicas isto vira uma corrida:
# o caminho correto passa a ser um Job de migração antes do deploy. O Alembic
# usa a tabela `alembic_version` com trava, então o risco é de duas réplicas
# esperarem, não de corromperem o schema — mas depender disso é frágil.
echo "[entrypoint] aplicando migrations..."
alembic upgrade head

# Idempotente: não faz nada se o usuário já existir. Sem
# ADMIN_INITIAL_PASSWORD o comando falha com mensagem explícita, e o contêiner
# não sobe — melhor que subir sem nenhum usuário e deixar o login respondendo
# "credenciais inválidas" para a senha certa.
if [ -n "${ADMIN_INITIAL_PASSWORD:-}" ]; then
    echo "[entrypoint] garantindo o administrador inicial..."
    python -m app.cli criar-admin
else
    echo "[entrypoint] ADMIN_INITIAL_PASSWORD não definida: nenhum usuário será criado." >&2
    echo "[entrypoint] o login ficará impossível até você criar um usuário." >&2
fi

echo "[entrypoint] iniciando: $*"
exec "$@"
