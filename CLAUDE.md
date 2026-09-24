# Sentinela — notas para quem for mexer

Plataforma de automação de contratos em Python/FastAPI/PostgreSQL. O README é
para quem vai **usar**; este arquivo é para quem vai **alterar**.

## Comandos

```bash
# venv desta máquina
.venv/Scripts/python.exe -m pytest -q          # 326 testes, 92% de cobertura
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format .
.venv/Scripts/python.exe -m mypy app
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check       # deriva entre modelo e banco

# tudo de uma vez (o mesmo que o CI roda)
./scripts/verificar.sh          # ou .\scripts\verificar.ps1
```

## Banco de desenvolvimento nesta máquina

Não é Docker: é o **PostgreSQL 18 nativo** em `D:\postgressSQL\18\bin`, que
precisa ser iniciado à mão e escuta na **porta 5433**.

```powershell
D:\postgressSQL\18\bin\pg_ctl.exe -D "D:\pgdata-beacon" `
    -l "D:\pgdata-beacon\server.log" -o "-p 5433" start
```

Bancos `sentinela` e `sentinela_test`, usuário `sentinela`/`sentinela`. O `.env`
local já aponta para lá.

Sem `TEST_DATABASE_URL`, os 91 testes de integração são **ignorados** com uma
mensagem — não falham. Se a contagem cair de 326 para 235, é isso.

## Docker nesta máquina

- **`docker compose` como subcomando quebra depois de todo restart do Docker
  Desktop**: o diretório `~/.docker/cli-plugins` é recriado vazio. Use
  `docker-compose` (binário standalone, já no PATH) ou copie o plugin de novo.
- **Bind mount de caminho Windows é recusado** pelo daemon. Por isso o
  `docker-compose.yml` não tem nenhum: tudo entra por `COPY`, e o único volume é
  nomeado.
- Se o daemon travar no meio de um build, o remendo conhecido é matar
  `Docker Desktop`, `com.docker.backend` e `com.docker.build`, renomear
  `%LOCALAPPDATA%\Docker\run` e `%LOCALAPPDATA%\docker-secrets-engine`,
  recriá-los vazios e reabrir o Docker Desktop.

## Decisões que não são óbvias pelo código

Leia [docs/DECISOES.md](docs/DECISOES.md) antes de "simplificar" qualquer um
destes pontos. Os que mais custaram:

1. **`RequestContext` é mutável de propósito.** Endpoints são `def` e rodam no
   threadpool; `anyio.to_thread.run_sync` **copia** o contexto por chamada, então
   um `ContextVar.set()` numa dependência não chega à seguinte. Tornar o contexto
   imutável de novo faz toda a auditoria voltar a sair sem autor, em silêncio.

2. **`rowcount` não vale para `INSERT ... ON CONFLICT` no psycopg3** — devolve
   `-1` nos dois casos, e `bool(-1)` é `True`. Use `RETURNING`. Vale para
   `add_if_absent` e `add_many_ignoring_duplicates`. Em `UPDATE` e `DELETE` o
   `rowcount` é confiável, e há um teste que fixa isso.

3. **`ON DELETE SET NULL` é incompatível com gatilho de imutabilidade.** O
   PostgreSQL implementa `SET NULL` como `UPDATE`, que o gatilho recusa. Por isso
   `audit_logs.actor_user_id` é `RESTRICT` (ADR-011). Se acrescentar gatilho a
   outra tabela, confira as chaves estrangeiras que apontam para ela.

4. **`try/except IntegrityError` não funciona no PostgreSQL.** O primeiro
   comando que falha aborta a transação inteira. Use `ON CONFLICT`. O teste com
   uma única duplicata **não** pega o defeito.

5. **A CSP quebra o Swagger UI.** `default-src 'none'` é o valor certo para a
   API e bloqueia o CSS e o JS do `/docs`. A exceção por caminho em
   `SecurityHeadersMiddleware` existe por isso. Não unifique.

6. **Mapeamento imperativo.** As entidades em `app/domain/entities.py` não
   herdam de `DeclarativeBase`. Quem liga entidade e tabela é
   `app/infrastructure/db/mappers.py`, e ele precisa ser chamado antes de
   qualquer consulta (`configure_mappers()`, idempotente).

7. **`alembic/env.py` só define a URL se ninguém já definiu.** Sobrescrever
   incondicionalmente faz as migrations rodarem no banco errado durante os
   testes, com sintoma de `relação "users" não existe`.

8. **`bcrypt_rounds` aceita mínimo 4** para a suíte rodar rápido; o piso de 12 é
   exigido pelo validador **só em produção**. Não suba o mínimo do campo sem
   ajustar a fixture.

9. **Não segure transação aberta durante chamada de rede.** O despacho de
   notificações comita **por item**, não por lote. Com o commit no fim, a
   transação fica ociosa durante os segundos de HTTP e o PostgreSQL a derruba
   pelo `idle_in_transaction_session_timeout` (30 s, em `session.py`). O sintoma
   é `server closed the connection unexpectedly` num INSERT que não tem nada a
   ver. Há um teste que fixa o comportamento; o timeout fica, porque foi ele que
   revelou o defeito.

## Onde mexer para cada coisa

| Quero... | Vá em |
|---|---|
| Acrescentar regra de negócio | `app/domain/rules.py` ou a entidade; nunca no serviço |
| Acrescentar endpoint | `app/api/v1/`, com `Depends(require(Permission.X))` — sem isso o teste de varredura reprova |
| Acrescentar permissão | `app/core/permissions.py`, e classificá-la em `WRITE_PERMISSIONS` ou não |
| Mudar o schema | `app/infrastructure/db/tables.py` **e** gerar migration; `alembic check` é o portão |
| Trocar o provedor de notificação | Implementar `NotificationGateway`; nada em `app/services/` muda |
| Acrescentar passo no job | `AutomationService._execute`, respeitando a ordem e o commit antes do despacho |

## Estado atual

- 326 testes verdes (235 unitários, 91 de integração), 92% de cobertura.
- `ruff check`, `ruff format --check`, `mypy app` e `alembic check` limpos.
- Pilha Docker construída e validada de ponta a ponta: migrations aplicadas,
  admin criado, job executado, 51 notificações entregues ao provedor simulado, e
  a segunda execução não gerou nenhuma duplicata.
- Publicado em **github.com/CaioCodes1/sentinela**, com o CI verde nos quatro jobs.

## Pendências conhecidas

- Métricas, rastreamento distribuído, MFA e retenção de auditoria estão
  listados como fora de escopo em `docs/AMBIENTE-CORPORATIVO.md`.
- O `.env` local aponta para o PostgreSQL da porta **5433** (nativo) enquanto o
  `docker-compose` sobe outro na **5432**. São bancos diferentes de propósito:
  um para a suíte, outro para a demonstração.
