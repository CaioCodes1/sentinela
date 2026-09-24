# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Imagem da API — multi-estágio.
#
# Por que dois estágios: as ferramentas de compilação (gcc, cabeçalhos) são
# necessárias para instalar algumas dependências e **não** são necessárias para
# executar. Deixá-las na imagem final aumenta o tamanho e, mais importante, dá
# a quem conseguir execução no contêiner um compilador à mão.
# ---------------------------------------------------------------------------

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# O `pyproject.toml` entra sozinho primeiro: enquanto ele não mudar, o Docker
# reaproveita a camada de dependências. Copiar o código junto invalidaria o
# cache a cada alteração de uma linha e reinstalaria tudo.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && mkdir -p app && touch app/__init__.py \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

# `libpq5` é a biblioteca de runtime do PostgreSQL; `libpq-dev` (com cabeçalhos
# e compilador) fica no estágio de build. `curl` serve ao HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# O pip do Python **do sistema** sai da imagem final.
#
# Não é higiene abstrata: o pip 25.0.1 da imagem base empacota cópias próprias
# de outras bibliotecas em `pip/_vendor/`, e são elas que aparecem no scanner —
# msgpack 1.1.2 e o `pkg_resources` do setuptools 70.3.0, duas HIGH. Nenhuma é
# dependência deste projeto: a aplicação roda do `/opt/venv`, que traz o seu
# próprio pip, mais novo e sem esses achados.
#
# Remover também fecha uma porta: contêiner com instalador de pacotes à mão dá
# a quem conseguir execução remota um jeito pronto de buscar ferramenta nova.
# É o mesmo motivo de o compilador ficar no estágio de build.
#
# Caminho absoluto de propósito: o `PATH` desta imagem já aponta para
# `/opt/venv/bin`, que só é copiado mais abaixo. Escrever `python` aqui
# funcionaria por acidente de ordem das camadas.
RUN /usr/local/bin/python -m pip uninstall -y pip setuptools wheel 2>/dev/null || true     && rm -rf /usr/local/lib/python3.12/site-packages/pip               /usr/local/lib/python3.12/site-packages/pip-*.dist-info

# Usuário sem privilégios, criado antes de copiar o código.
#
# Contêiner que roda como root significa que uma falha de execução remota na
# aplicação começa como root dentro do contêiner — e o caminho daí para o host
# é bem mais curto. É a correção mais barata de segurança de contêiner que
# existe, e a mais esquecida.
RUN groupadd --gid 10001 sentinela \
    && useradd --uid 10001 --gid sentinela --create-home --shell /usr/sbin/nologin sentinela

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=sentinela:sentinela app ./app
COPY --chown=sentinela:sentinela alembic ./alembic
COPY --chown=sentinela:sentinela alembic.ini pyproject.toml ./
COPY --chown=sentinela:sentinela docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

USER sentinela

EXPOSE 8000

# A sonda usa `/health/live`, que **não** toca no banco. Se usasse readiness,
# uma indisponibilidade do PostgreSQL faria o orquestrador matar e recriar a
# API em laço — remédio errado para o problema.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health/live || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
