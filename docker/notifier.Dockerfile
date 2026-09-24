# syntax=docker/dockerfile:1.7
# Provedor de notificações simulado. Não faz parte da aplicação — existe para
# que o retry, o timeout e o disjuntor do gateway possam ser vistos falhando
# contra um serviço HTTP de verdade.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9"

RUN groupadd --gid 10002 stub \
    && useradd --uid 10002 --gid stub --create-home --shell /usr/sbin/nologin stub

WORKDIR /app
# COPY, e não bind mount: além de ser o certo para uma imagem, o Docker Desktop
# desta máquina de desenvolvimento recusa bind mount de caminho Windows.
COPY --chown=stub:stub notifier_stub ./notifier_stub

USER stub
EXPOSE 9090

CMD ["uvicorn", "notifier_stub.main:app", "--host", "0.0.0.0", "--port", "9090"]
