# Arquitetura

## 1. O princípio que organiza tudo

**As dependências apontam para dentro.** O domínio não sabe que existe FastAPI,
não sabe que existe SQLAlchemy e não sabe o que é uma requisição HTTP. Quem se
adapta é a infraestrutura.

Isso não é preferência estética. É o que torna possível:

- testar a regra de negócio **sem banco** — 274 dos 322 testes rodam em ~2s;
- trocar o provedor de notificação escrevendo uma classe, sem tocar em serviço;
- chamar o mesmo caso de uso a partir de HTTP, do agendador e da linha de
  comando, sem duplicar nada.

```
    ┌──────────────────────────────────────────────┐
    │  app/api        HTTP, schemas, middlewares   │
    ├──────────────────────────────────────────────┤
    │  app/services   casos de uso, transação      │
    ├──────────────────────────────────────────────┤
    │  app/domain     entidades, regras, PORTAS    │  ← ninguém abaixo
    ├──────────────────────────────────────────────┤
    │  app/infra      implementa as portas         │  ← depende do domínio
    └──────────────────────────────────────────────┘
```

A seta entre `services` e `infrastructure` **não** existe diretamente: o serviço
depende de `app/domain/ports/`, e a infraestrutura implementa aquelas
interfaces. É a inversão de dependência do "D" de SOLID.

## 2. Cada camada em detalhe

### `app/domain` — o núcleo

Nenhum `import` de biblioteca externa além da biblioteca padrão.

| Arquivo | Papel |
|---|---|
| `entities.py` | `User`, `Client`, `Contract`, `Installment`, `Notification`, `AuditLog`, `JobRun`. Classes puras, com as invariantes e as transições de estado |
| `value_objects.py` | `TaxId` (CPF/CNPJ com dígito verificador), `EmailAddress`, `Money` (`Decimal`, nunca `float`) |
| `rules.py` | Funções puras: aritmética de meses, plano de renovação, geração de parcelas, limiares de alerta |
| `enums.py` | Estados e ações, como texto — nunca índice numérico |
| `exceptions.py` | Erros de domínio com `code` estável, sem conhecer HTTP |
| `ports/` | `Protocol` para repositórios, unidade de trabalho e notificador |

**Por que objetos de valor.** Enquanto o CPF for `str`, qualquer string entra no
banco e a validação depende de alguém lembrar de chamá-la. Sendo `TaxId`, o
único jeito de existir uma instância é tendo passado pelo dígito verificador.

**Por que funções puras recebem a data de referência.** `days_until_expiration(
end_date, reference)` em vez de chamar `date.today()` por dentro. É o que torna
a virada de mês testável sem congelar o relógio do processo — e o que evita o
clássico "o teste passa até 29 de fevereiro".

### `app/services` — casos de uso

Cada método público é um caso de uso e **uma transação**. O `commit` aparece uma
vez, no fim, nunca dentro de um laço.

"Criar contrato" grava o contrato, gera as parcelas, escreve o evento de
histórico e a linha de auditoria — e ou tudo isso acontece, ou nada acontece.
Sem esse limite explícito, cada repositório comita por conta própria e uma falha
no meio deixa contrato sem parcela e auditoria mentindo que deu certo.

### `app/infrastructure` — adaptadores

| Subpasta | Conteúdo |
|---|---|
| `db/tables.py` | Tabelas em estilo imperativo, com `CHECK`, índices e comentários |
| `db/types.py` | `TypeDecorator` que converte os objetos de valor na fronteira do ORM |
| `db/mappers.py` | Mapeamento imperativo: liga entidades puras às tabelas |
| `db/session.py` | Engine único por processo, com endurecimento por conexão |
| `db/uow.py` | Unidade de trabalho; traduz `StaleDataError` em erro de domínio |
| `repositories/` | Implementações das portas |
| `gateways/http_notifier.py` | Cliente HTTP com retry, jitter e disjuntor |

**Endurecimento por conexão.** Toda conexão nova recebe `SET TIME ZONE 'UTC'`,
`search_path` explícito, `statement_timeout=15s` e
`idle_in_transaction_session_timeout=30s`. O `search_path` fixo impede que um
schema no caminho de busca sombreie uma função usada pelas consultas; os
timeouts transformam uma consulta patológica em erro localizado, em vez de uma
conexão presa segurando o pool inteiro.

**`pool_pre_ping=True`.** Sem ele, toda vez que o banco reinicia (ou um firewall
derruba conexão ociosa) a primeira requisição de cada conexão do pool falha com
"server closed the connection unexpectedly" — erro que só aparece em produção.

### `app/api` — entrada HTTP

Traduz HTTP em chamada de caso de uso. Não contém regra de negócio e não acessa
o banco direto.

**Ordem dos middlewares.** O Starlette os executa na ordem **inversa** do
registro. A sequência efetiva é:

1. `RequestContext` — gera o `request_id`, mede a duração
2. `RateLimit` — barra antes de qualquer trabalho, inclusive antes do banco
3. `BodySizeLimit` — recusa corpo grande antes de ler
4. `SecurityHeaders` — carimba a resposta na volta
5. `CORS` — por último

O contexto precisa ser o primeiro porque o log do rate limit já usa o
`request_id`. Invertido, a requisição barrada apareceria no log sem
identificação — justamente a que se quer rastrear.

## 3. O contexto da requisição, e uma armadilha

A linha de auditoria precisa de IP, user-agent, id da requisição e autor. Quem
grava a auditoria é o serviço, que não deveria conhecer `Request`. A alternativa
seria passar cinco parâmetros por toda a cadeia até o repositório.

A solução é `contextvars`, que ao contrário de uma variável global é isolado por
tarefa **e** por thread.

**A armadilha, que custou um defeito real.** Os endpoints são `def`, então o
FastAPI executa cada dependência e cada handler no threadpool, via
`anyio.to_thread.run_sync` — que **copia** o contexto para cada chamada. Um
`ContextVar.set()` feito dentro de uma dependência morre ali: a dependência
seguinte recebe outra cópia.

Na primeira versão, `get_current_user` resolvia o usuário e chamava
`set_context(ctx.with_actor(...))`, e mesmo assim **toda linha de auditoria saía
com `actor_email` nulo**. Sem erro, sem aviso — só uma trilha inútil, que é o
pior defeito possível numa trilha de auditoria.

A correção: `RequestContext` é **mutável**, e a cópia do contexto compartilha o
mesmo objeto. Copiar um `Context` copia o mapa `var → valor`; o valor continua
sendo a mesma instância. Mutar o objeto é visível em todas as cópias; trocar a
referência do `ContextVar`, não.

## 4. Fluxo de execução

### Requisição HTTP

```
Cliente
  → RequestContextMiddleware      abre contexto, gera request_id
  → RateLimitMiddleware           120/min por IP
  → BodySizeLimitMiddleware       256 KB
  → rota do FastAPI
      → get_uow()                 abre sessão e transação
      → get_current_user()        valida JWT, relê o usuário, registra o autor
      → require(Permission.X)     autoriza; negativa vira 403 + auditoria
      → schema Pydantic           valida entrada, recusa campo desconhecido
      → serviço                   caso de uso + commit
  → SecurityHeadersMiddleware     carimba a resposta
Cliente
```

### Job agendado

```
APScheduler (03:00 UTC)
  → run_daily_check()             monta as próprias dependências
      → AutomationService.run_daily()
          → contexto de sistema    autor = system:<job>
          → trava consultiva       se não obtiver: SKIPPED
          → passos 1..3 + COMMIT
          → passo 4: despacho
          → JobRun + auditoria
```

### Linha de comando

```
python -m app.cli <comando>
  → get_settings() → init_engine() → UoW → serviço
```

Os três caminhos usam **o mesmo** código de domínio e de serviço.

## 5. Justificativa de cada tecnologia

| Tecnologia | Por que ela, e não outra |
|---|---|
| **Python 3.12** | `StrEnum`, sintaxe de união com `\|`, melhor desempenho; ecossistema padrão em automação e integração |
| **FastAPI** | Validação e OpenAPI derivados do mesmo tipo — o schema não pode divergir da implementação, que é a causa mais comum de documentação de API errada. Injeção de dependência nativa, usada aqui para transação, autenticação e autorização |
| **Pydantic v2** | Núcleo em Rust; `extra="forbid"` transforma atribuição em massa em erro visível; `Decimal` preservado de verdade |
| **SQLAlchemy 2.0** | ORM com escotilha para SQL quando preciso (`ON CONFLICT`, advisory lock, índice parcial). Mapeamento imperativo, que é o que mantém o domínio puro |
| **PostgreSQL 16** | `JSONB` para o `details` da auditoria, índice parcial e funcional, `ON CONFLICT`, trava consultiva, gatilho para imutabilidade. Nenhum desses existe em MySQL com a mesma qualidade, e nenhum existe em SQLite |
| **psycopg 3** | Driver atual, com suporte a pipeline e tipagem melhor que o psycopg2 |
| **Alembic** | Migrations versionadas com `downgrade`, e `alembic check` como portão de deriva |
| **PyJWT** | Biblioteca focada; permite exigir explicitamente `iss`, `aud` e a lista de algoritmos |
| **bcrypt (direto)** | Sem `passlib` no meio: uma dependência a menos e nenhum aviso de compatibilidade entre versões |
| **httpx** | Timeout separado por fase, o que um timeout total não distingue |
| **APScheduler** | Agendamento no processo; ver [ADR-007](DECISOES.md#adr-007) |
| **Pytest** | Fixtures compostas, parametrização, marcadores — o que permite separar a suíte rápida da que exige banco |
| **Ruff** | Linter e formatador num binário; inclui as regras do Bandit (`S`), que são de segurança |
| **mypy** | Tipagem estática onde o custo é baixo e o retorno é alto: as fronteiras |

## 6. Padrões aplicados

**Repository.** Acesso a dados atrás de interface. A prova de que a abstração é
real, e não decorativa, está em `tests/fakes.py`: os dublês em memória
satisfazem as mesmas portas e sustentam a suíte unitária inteira.

**Unit of Work.** A transação é explícita e tem dono. O `__exit__` faz rollback
incondicional — se `commit()` já rodou, não tem efeito; se ninguém comitou, ele
desfaz. É a rede que impede uma escrita parcial de ficar pendurada na conexão
devolvida ao pool.

**Service Layer.** Orquestração separada de regra. As regras que valem sozinhas
estão em `rules.py` e nas entidades; o serviço as compõe.

**Dependency Injection.** Container explícito (`AppContainer`) em vez de
variáveis de módulo espalhadas. No teste, `app.dependency_overrides` troca
qualquer peça sem tocar em nenhuma rota.

**Ports & Adapters.** `NotificationGateway`, `RateLimiterPort` e os repositórios
são portas. Trocar o provedor de notificação por uma fila é escrever outra
classe.

## 7. Onde a arquitetura foi dobrada, e por quê

Nenhuma arquitetura sobrevive intacta ao contato com o problema. Os pontos em
que este projeto abriu exceção, de propósito:

- **`AuditService.record_isolated` comita por conta própria.** Viola a regra de
  "um commit por caso de uso". Existe para a falha de login, que termina em
  exceção — sem ele, o rollback levaria embora justamente o registro de força
  bruta.
- **`get_current_user` consulta o banco.** Uma dependência de autenticação
  "pura" só validaria o token. A consulta existe para que desativar um usuário
  tenha efeito imediato.
- **O `AutomationService` chama `ContractService`.** Serviço chamando serviço é
  um cheiro. Aqui é deliberado: a renovação automática precisa ser *exatamente*
  a mesma operação da renovação manual, incluindo histórico e auditoria.
  Duplicar a lógica seria pior.

Cada uma dessas exceções está comentada no código, no ponto em que acontece.
