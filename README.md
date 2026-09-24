# Sentinela

**Plataforma de automação de contratos, mensalidades e renovações**, construída
com segurança, auditoria e rastreabilidade como requisito de primeira classe —
não como camada aplicada no fim.

O sistema identifica contratos próximos ao vencimento, transiciona status,
gera mensalidades, dispara notificações por um provedor externo com retry e
disjuntor, e registra **quem fez o quê, quando, de onde e sobre qual recurso**
numa trilha que o próprio banco de dados impede de alterar.

```
Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · Docker
JWT com rotação de refresh · RBAC · bcrypt · Pytest · Ruff · mypy
```

| | |
|---|---|
| **Testes** | 323 (232 unitários + 91 de integração), todos verdes · 92% de cobertura |
| **Endpoints** | 26, todos documentados em OpenAPI |
| **Migrations** | 3, incluindo gatilho de imutabilidade e índices funcionais |
| **Qualidade estática** | `ruff check`, `ruff format` e `mypy app` sem apontamentos |
| **Deriva de schema** | `alembic check` sem diferenças entre modelo e banco |

---

## Índice

1. [Por que este projeto existe](#por-que-este-projeto-existe)
2. [Começando em 3 minutos](#começando-em-3-minutos)
3. [Arquitetura](#arquitetura)
4. [Estrutura de diretórios](#estrutura-de-diretórios)
5. [Segurança](#segurança)
6. [Banco de dados](#banco-de-dados)
7. [Automação](#automação)
8. [Integração com serviço externo](#integração-com-serviço-externo)
9. [API](#api)
10. [Testes](#testes)
11. [Qualidade de código](#qualidade-de-código)
12. [Documentação complementar](#documentação-complementar)
13. [O que foi deixado de fora, e por quê](#o-que-foi-deixado-de-fora-e-por-quê)

---

## Por que este projeto existe

Controle de contratos é um problema chato e caro nas empresas que dependem
dele. Um contrato que vence sem aviso vira serviço prestado sem cobertura
contratual; uma mensalidade gerada em duplicidade vira estorno e ligação para o
cliente; uma alteração de valor sem registro de autoria vira discussão que
ninguém consegue resolver.

Os três problemas têm a mesma raiz: **o sistema não sabe contar a própria
história**. A resposta deste projeto é tratar registro e rastreabilidade como
requisito funcional, e não como log.

### O mesmo domínio, em outra linguagem

Este domínio — contratos, vencimento, renovação — também está resolvido em
[vigencia](https://github.com/CaioCodes1/vigencia), em **Java/Spring**. A
repetição é deliberada: o que interessa são as decisões que **mudam quando o
ecossistema muda**. Aqui, o mapeamento imperativo do SQLAlchemy mantém o domínio
sem nenhuma dependência de framework, e é isso que permite a 232 dos 323 testes
rodarem sem banco; no Java, o mesmo objetivo pede outro arranjo. Comparar os dois
diz mais sobre o critério de cada escolha do que qualquer um deles isolado.

---

## Começando em 3 minutos

### Com Docker (recomendado)

```bash
cp .env.example .env
```

Preencha no `.env`, no mínimo:

```bash
JWT_SECRET=...              # python -m app.cli gerar-segredo
POSTGRES_PASSWORD=...
NOTIFIER_API_KEY=...
ADMIN_INITIAL_PASSWORD=...  # mínimo 12 caracteres, com maiúscula, dígito e símbolo
```

> A pilha **recusa subir** se qualquer uma dessas faltar. Não é rigor
> decorativo: sem `ADMIN_INITIAL_PASSWORD` nenhum usuário é criado, e o login
> responderia "credenciais inválidas" para a senha certa — meia hora de
> investigação para quem clonasse o repositório.

```bash
docker compose up -d --build
docker compose logs -f api
```

- API: <http://localhost:8000>
- Documentação interativa: <http://localhost:8000/docs>
- Provedor de notificações simulado: <http://localhost:9090/docs>

```bash
# Dados de demonstração, com contratos vencendo em 30, 7 e 1 dia
docker compose exec api python -m app.cli semear-demo

# Dispara a automação na hora, sem esperar as 3h da manhã
docker compose exec api python -m app.cli rodar-job

# Veja o que o "provedor" recebeu
curl http://localhost:9090/v1/messages
```

### Sem Docker

Requer Python 3.11+ e um PostgreSQL acessível.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip

cp .env.example .env                   # ajuste DATABASE_URL
alembic upgrade head
python -m app.cli criar-admin

uvicorn app.main:app --reload
```

### Primeira chamada

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@sentinela.local","password":"SUA_SENHA"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 900,
  "role": "ADMIN"
}
```

---

## Arquitetura

### A forma

Arquitetura em camadas com **inversão de dependência**: as setas apontam para
dentro. O domínio não conhece FastAPI, não conhece SQLAlchemy e não conhece
HTTP. Quem se adapta é a infraestrutura.

```mermaid
flowchart TB
    subgraph EXT["Mundo externo"]
        HTTP["Cliente HTTP"]
        CRON["Agendador<br/>(APScheduler)"]
        CLI["Linha de comando"]
        PROV["Provedor de<br/>notificações"]
        DB[("PostgreSQL")]
    end

    subgraph API["app/api — entrada"]
        ROUTES["Rotas + schemas Pydantic"]
        DEPS["Dependências:<br/>autenticação, RBAC, UoW"]
        MW["Middlewares:<br/>contexto, cabeçalhos, rate limit"]
    end

    subgraph SVC["app/services — casos de uso"]
        S1["ContractService"]
        S2["ClientService"]
        S3["AuthService"]
        S4["AutomationService"]
        S5["NotificationService"]
        S6["AuditService"]
    end

    subgraph DOM["app/domain — o núcleo"]
        ENT["Entidades<br/>(sem SQLAlchemy)"]
        RUL["Regras puras"]
        VO["Objetos de valor"]
        PORT["Portas<br/>(Protocol)"]
    end

    subgraph INF["app/infrastructure — adaptadores"]
        REPO["Repositórios<br/>SQLAlchemy"]
        UOW["Unidade de trabalho"]
        GW["Gateway HTTP<br/>retry + disjuntor"]
        MAP["Mapeamento imperativo"]
    end

    HTTP --> MW --> ROUTES --> DEPS --> SVC
    CRON --> S4
    CLI --> SVC
    SVC --> DOM
    SVC -.->|depende da abstração| PORT
    REPO -.->|implementa| PORT
    GW -.->|implementa| PORT
    REPO --> UOW --> DB
    MAP --- ENT
    GW --> PROV

    style DOM fill:#1e3a5f,stroke:#4a90d9,color:#fff
    style SVC fill:#2d4a22,stroke:#6aa84f,color:#fff
    style INF fill:#4a3520,stroke:#d9974a,color:#fff
    style API fill:#3d2a4a,stroke:#a86ad9,color:#fff
```

### Responsabilidade de cada camada

| Camada | Responsabilidade | O que ela **não** pode fazer |
|---|---|---|
| `app/domain` | Entidades, regras de negócio, objetos de valor e as **portas** (interfaces) que ela exige do mundo | Importar SQLAlchemy, FastAPI, httpx ou qualquer biblioteca de infraestrutura |
| `app/services` | Orquestrar casos de uso; define a fronteira da transação | Conhecer HTTP, `Request`, status code |
| `app/infrastructure` | Implementar as portas: repositórios, gateways, mapeamento, unidade de trabalho | Conter regra de negócio |
| `app/api` | Traduzir HTTP em chamada de caso de uso; autenticar, autorizar, validar e serializar | Conter regra de negócio ou acessar o banco direto |
| `app/jobs` | Disparar casos de uso em horário agendado | Conter lógica — ela mora em `AutomationService` |

### A decisão mais incomum: mapeamento imperativo

As entidades em `app/domain/entities.py` são classes Python puras. A ligação
com as tabelas é feita de fora, em `app/infrastructure/db/mappers.py`, com
`registry.map_imperatively`.

Quase todo tutorial de FastAPI usa `DeclarativeBase`, o que obriga a entidade a
herdar de uma classe do ORM. A partir daí, a regra de negócio só roda com o
SQLAlchemy configurado: um teste de "contrato cancelado não pode ser renovado"
passa a precisar de banco.

Aqui ele é `Contract(...)` e uma chamada de método. É por isso que 232 dos 323
testes rodam em **6 segundos**, sem Docker e sem migration. O custo e as
alternativas estão no [ADR-002](docs/DECISOES.md#adr-002).

### Fluxo de uma requisição

```mermaid
sequenceDiagram
    participant C as Cliente
    participant MW as Middlewares
    participant DEP as Dependências
    participant SVC as Serviço
    participant UOW as Unidade de trabalho
    participant DB as PostgreSQL

    C->>MW: POST /api/v1/contracts + Bearer
    MW->>MW: gera request_id, mede tempo
    MW->>MW: rate limit por IP (barra antes de tocar no banco)
    MW->>DEP: segue
    DEP->>DEP: valida JWT (assinatura, exp, iss, aud, typ)
    DEP->>DB: carrega o usuário (papel vem do banco, não do token)
    DEP->>DEP: RBAC — exige contract:create
    Note over DEP: negativa vira 403 **e** linha de auditoria
    DEP->>SVC: chama o caso de uso
    SVC->>UOW: abre a transação
    SVC->>UOW: contrato + parcelas + histórico + auditoria
    UOW->>DB: COMMIT (tudo ou nada)
    SVC-->>C: 201 Created
    MW-->>C: + cabeçalhos de segurança + X-Request-Id
```

---

## Estrutura de diretórios

```
sentinela/
├── app/
│   ├── main.py                      # monta o app: container, middlewares, rotas
│   ├── cli.py                       # criar-admin, rodar-job, gerar-segredo, semear-demo
│   │
│   ├── core/                        # infraestrutura transversal
│   │   ├── config.py                # Settings; recusa placeholder em produção
│   │   ├── security.py              # bcrypt, JWT, política de senha
│   │   ├── permissions.py           # a matriz de RBAC, num arquivo só
│   │   ├── context.py               # contexto da requisição (contextvars)
│   │   ├── logging.py               # JSON + redação de dado pessoal
│   │   ├── errors.py                # RFC 7807; traduz exceção em resposta
│   │   └── rate_limit.py            # janela deslizante
│   │
│   ├── domain/                      # o núcleo — sem dependência externa
│   │   ├── entities.py              # User, Client, Contract, Installment, ...
│   │   ├── value_objects.py         # TaxId (CPF/CNPJ), EmailAddress, Money
│   │   ├── rules.py                 # datas, renovação, parcelas, limiares
│   │   ├── enums.py
│   │   ├── exceptions.py            # erros de domínio, sem HTTP
│   │   └── ports/                   # Protocol: repositórios, UoW, notificador
│   │
│   ├── services/                    # casos de uso; a transação começa aqui
│   ├── infrastructure/
│   │   ├── db/                      # tabelas, tipos, mapeamento, sessão, UoW
│   │   ├── repositories/            # implementações das portas
│   │   └── gateways/http_notifier.py# retry, backoff com jitter, disjuntor
│   ├── api/
│   │   ├── deps.py                  # injeção, autenticação, require(Permission)
│   │   ├── middleware.py
│   │   ├── schemas/                 # entrada e saída, separados
│   │   └── v1/                      # auth, clients, contracts, operations, audit
│   └── jobs/                        # agendador e a função agendada
│
├── alembic/versions/                # 3 migrations, com o porquê de cada uma
├── notifier_stub/                   # o "terceiro" fictício, com falhas reais
├── docker/                          # Dockerfiles e entrypoint
├── docs/                            # arquitetura, segurança, banco, API, ADRs
├── scripts/                         # verificação local (mesmos passos do CI)
└── tests/
    ├── fakes.py                     # dublês em memória das portas
    ├── unit/                        # 274 testes, sem banco
    └── integration/                 # 48 testes, com PostgreSQL de verdade
```

---

## Segurança

Detalhamento completo em [docs/SEGURANCA.md](docs/SEGURANCA.md). Resumo do que
está implementado e **por quê**:

### Autenticação

| Mecanismo | Detalhe |
|---|---|
| Access token | JWT HS256, 15 minutos, com `jti`, `iss`, `aud` e `typ` verificados |
| Refresh token | 7 dias, **rotativo**, guardado como SHA-256 |
| Detecção de reúso | Token revogado reapresentado → **a família inteira cai** |
| Senhas | bcrypt com custo 12, sal por hash, limite de 72 bytes respeitado |
| Enumeração de usuário | Mensagem única e **tempo constante** (bcrypt falso quando o e-mail não existe) |
| Força bruta | Rate limit por IP no login + bloqueio de conta após 5 tentativas |

A rotação com detecção de reúso é o ponto central. Sem ela, um refresh token
capturado vale sete dias de acesso sem deixar rastro. Com ela, o segundo uso
denuncia o roubo e derruba a sessão das duas partes.

```mermaid
sequenceDiagram
    participant U as Usuário
    participant A as API
    participant D as Banco

    U->>A: POST /auth/login
    A->>D: grava refresh #1 (hash, família F)
    A-->>U: access + refresh #1

    U->>A: POST /auth/refresh (#1)
    A->>D: revoga #1, grava #2 (mesma família F)
    A-->>U: access + refresh #2

    Note over U,A: um atacante capturou o #1 e tenta usar

    U->>A: POST /auth/refresh (#1, já revogado)
    A->>D: REVOGA A FAMÍLIA F INTEIRA
    A->>D: audita TOKEN_REUSE_DETECTED
    A-->>U: 401 — sessão encerrada por segurança
```

### Autorização (RBAC)

Três perfis, uma matriz central em `app/core/permissions.py`. O endpoint
declara a permissão que exige; nenhum handler consulta `user.role`.

| Permissão | ADMIN | OPERATOR | AUDITOR |
|---|:---:|:---:|:---:|
| `client:create` · `client:update` · `client:deactivate` | ✅ | ✅ | ❌ |
| `contract:create` · `update` · `cancel` · `renew` | ✅ | ✅ | ❌ |
| `installment:settle` · `notification:retry` | ✅ | ✅ | ❌ |
| *todas as leituras* | ✅ | ✅ | ✅ |
| `audit:read` | ✅ | ❌ | ✅ |
| `job:run` | ✅ | ❌ | ❌ |
| `user:create` | ✅ | ❌ | ❌ |

Três consequências de a matriz ser central, e não espalhada:

1. `GET /api/v1/admin/permissions` publica a matriz **viva** — documento de
   permissões envelhece, este endpoint não pode divergir.
2. O teste `test_auditor_nunca_escreve` verifica uma **propriedade**, não uma
   lista: permissão de escrita criada amanhã e atribuída ao auditor por engano
   reprova sozinha.
3. `test_toda_rota_exige_autorizacao` varre o app montado e reprova qualquer
   rota que tenha entrado sem proteção.

Note o que **não** existe: permissão de apagar auditoria. Se existisse,
bastaria promover alguém a ADMIN para apagar o próprio rastro.

### Auditoria imutável

A tabela `audit_logs` é protegida por um gatilho `BEFORE UPDATE OR DELETE` que
recusa qualquer alteração — **inclusive vinda de superusuário e do dono da
tabela**.

`REVOKE UPDATE, DELETE` seria a resposta mais óbvia e tem duas falhas: em
desenvolvimento e teste a conexão costuma ser superusuário (e superusuário
ignora permissão, então o teste passa verde sem testar nada), e o dono da
tabela pode se reconceder o que perdeu com uma linha de `GRANT`.

```sql
-- o que acontece com quem tentar, inclusive no psql
UPDATE audit_logs SET action = 'LOGOUT';
-- ERRO: a tabela audit_logs é somente-inserção: UPDATE não é permitido
-- DICA: Trilhas de auditoria e histórico não podem ser alteradas nem removidas.
```

A porta de saída legítima é `TRUNCATE`, que não dispara gatilho de linha —
previsto para política de retenção e para limpeza entre testes.

Cada linha responde às cinco perguntas de uma investigação: **quem**
(`actor_user_id` + `actor_email` e `actor_role` copiados do momento do ato),
**o quê** (`action`), **quando**, **de onde** (`ip_address`) e **sobre o quê**
(`resource_type` + `resource_id`), mais o `request_id` que amarra tudo ao log.

### Proteção de dados

- **Nada de SQL concatenado.** Toda consulta é construída pelo SQLAlchemy com
  parâmetros ligados. O termo de busca ainda tem os curingas do `LIKE`
  escapados — sem isso, buscar por `%` devolveria a base inteira.
- **Redação de dado pessoal no log.** Um filtro roda em *todo* registro,
  inclusive nos de biblioteca de terceiro: CPF, CNPJ, e-mail, JWT, hash bcrypt
  e cabeçalho `Authorization` saem redigidos. Log é o vazamento mais comum e o
  menos notado.
- **Mascaramento na auditoria e nas respostas.** Documento vira `***.***.247-25`;
  destinatário de notificação vira `f***@empresa.com`.
- **Erro que não conta demais.** `IntegrityError` do psycopg traz nome de
  tabela, de restrição e o valor que colidiu. Vai para o log; a resposta diz
  "conflito com o estado atual" e o `request_id` que liga as duas pontas.
- **Erro de schema sem eco do valor.** O erro cru do Pydantic inclui `input` —
  num login malformado, isso é a senha digitada dentro do corpo da resposta.

### Cabeçalhos e limites

`Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`,
`Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store`, e HSTS
apenas em produção.

> A CSP correta para uma API JSON é `default-src 'none'`. Só que o Swagger UI
> **é** HTML servido pela mesma aplicação, e com essa política ele abre em
> branco — o CSS e o JS da própria documentação são bloqueados. Nenhum teste de
> API pega isso, porque teste de API não renderiza página. A solução aqui é CSP
> estrita para tudo, com uma exceção declarada para os caminhos de `/docs`.

Rate limit em janela deslizante: 120/min geral por IP, 10/min no login.
Corpo de requisição limitado a 256 KB.

### Segredos

`.env` fora do controle de versão e fora da imagem Docker. Com
`APP_ENV=production` a aplicação **recusa subir** se o `JWT_SECRET` ainda for o
valor de exemplo, se ele tiver menos de 32 caracteres, se `BCRYPT_ROUNDS` for
menor que 12, se `DEBUG` estiver ligado ou se `CORS_ORIGINS` for `*`.

---

## Banco de dados

Modelagem completa, com a explicação de cada tabela e de cada índice, em
[docs/BANCO-DE-DADOS.md](docs/BANCO-DE-DADOS.md).

```mermaid
erDiagram
    USERS ||--o{ REFRESH_TOKENS : "sessões"
    USERS ||--o{ AUDIT_LOGS : "autoria (RESTRICT)"
    CLIENTS ||--o{ CONTRACTS : "titular (RESTRICT)"
    CONTRACTS ||--o{ INSTALLMENTS : "mensalidades (CASCADE)"
    CONTRACTS ||--o{ CONTRACT_EVENTS : "histórico (CASCADE)"
    CONTRACTS ||--o{ NOTIFICATIONS : "avisos (CASCADE)"
    CONTRACTS ||--o| CONTRACTS : "renovado_para"

    USERS {
        uuid id PK
        varchar email UK "único por lower(email)"
        varchar password_hash "bcrypt; CHECK impede texto puro"
        varchar role "ADMIN | OPERATOR | AUDITOR"
        int failed_login_attempts
        timestamptz locked_until
    }
    CONTRACTS {
        uuid id PK
        varchar number UK
        numeric monthly_amount "NUMERIC(14,2), nunca float"
        date start_date
        date end_date
        int due_day "1..31, resolvido por mês"
        varchar status
        int version "trava otimista"
    }
    INSTALLMENTS {
        uuid id PK
        varchar competence "AAAA-MM"
        date due_date
        numeric amount
        varchar status
    }
    NOTIFICATIONS {
        uuid id PK
        varchar dedupe_key UK "idempotência da automação"
        int attempts
        varchar status "PENDING|SENT|FAILED|DEAD_LETTER"
    }
    AUDIT_LOGS {
        uuid id PK
        timestamptz occurred_at
        varchar action
        varchar actor_email "cópia do momento do ato"
        varchar ip_address
        jsonb details
    }
```

Nove tabelas, com destaque para as decisões que não são óbvias:

- **`NUMERIC(14,2)`, jamais `float`.** `SELECT 0.1::float8 + 0.2::float8`
  devolve `0.30000000000000004`. Somado em doze parcelas, isso é um centavo que
  ninguém consegue explicar para o financeiro.
- **`TIMESTAMPTZ` em tudo.** `TIMESTAMP` sem fuso é a origem do contrato que
  vence uma hora mais tarde entre outubro e fevereiro.
- **Índices parciais** para as consultas que importam: contratos abertos por
  vencimento, parcelas em aberto, notificações a enviar. Contrato encerrado é a
  maioria da tabela com o tempo, e nunca entra nessas consultas.
- **`CHECK` de coerência**, não só de faixa: contrato `CANCELLED` sem
  `cancelled_at` é recusado, porque senão o relatório de cancelamentos do mês
  deixa de contá-lo em silêncio.
- **`ON DELETE` escolhido por tabela**: `CASCADE` onde o filho é parte do pai
  (parcelas), `RESTRICT` onde apagar destruiria história (cliente, usuário).

### Migrations

```bash
alembic upgrade head                        # aplica
alembic downgrade -1                        # reverte a última
alembic revision --autogenerate -m "..."    # gera a partir do modelo
alembic check                               # há deriva entre modelo e banco?
```

`alembic check` é um portão de verdade neste projeto: os índices funcionais e
os comentários estão declarados no modelo, não só nas migrations — um índice
que existisse apenas na migration apareceria como "removido" a cada execução e
transformaria o portão em ruído que todo mundo aprende a ignorar.

Migrations **não importam código da aplicação**: um `render_item` em
`alembic/env.py` escreve os tipos personalizados como o tipo SQL que eles são.
Renomear uma classe amanhã não pode quebrar o registro do que já foi aplicado.

---

## Automação

```mermaid
flowchart TD
    START(["03:00 UTC — APScheduler"]) --> LOCK{"pg_try_advisory_xact_lock"}
    LOCK -->|já tomada| SKIP["registra SKIPPED e sai<br/>(não é erro)"]
    LOCK -->|obtida| RUN["cria JobRun + audita JOB_STARTED"]

    RUN --> P1["1 · contratos vigentes"]
    P1 --> P1A{"faltam exatamente<br/>30, 15, 7 ou 1 dia?"}
    P1A -->|sim| P1B["enfileira alerta<br/>(dedupe_key por limiar)"]
    P1A -->|não| P2
    P1B --> P2["2 · já passou do término?"]

    P2 -->|sim, auto_renew| P2A["cria contrato sucessor"]
    P2 -->|sim| P2B["marca EXPIRED + avisa"]
    P2 -->|não, dentro da janela| P2C["marca EXPIRING"]

    P2A --> P3
    P2B --> P3
    P2C --> P3["3 · parcelas vencidas → OVERDUE + cobrança"]
    P3 --> CM1[("COMMIT")]
    CM1 --> P4["4 · despacha a fila de notificações"]
    P4 --> P5["5 · finaliza JobRun + audita JOB_FINISHED"]
    P5 --> END(["fim"])

    style LOCK fill:#4a3520,stroke:#d9974a,color:#fff
    style CM1 fill:#1e3a5f,stroke:#4a90d9,color:#fff
    style SKIP fill:#4a2020,stroke:#d94a4a,color:#fff
```

Duas propriedades sustentam essa rotina:

**Idempotência.** Rodar duas vezes no mesmo dia não gera nada em dobro. Cada
notificação tem uma `dedupe_key` única (`CONTRACT_EXPIRING:<id>:d30`) e cada
parcela tem `(contrato, competência)` única. A garantia está no **índice do
banco**, não na memória do processo — dois processos concorrentes não
compartilham memória, mas compartilham o banco. É o que permite reprocessar um
dia que falhou sem ligar para o cliente pedindo desculpa pelos e-mails
repetidos.

**Exclusão mútua.** Uma trava consultiva do PostgreSQL impede duas réplicas de
rodarem juntas. A variante `xact` é liberada no fim da transação mesmo se o
processo morrer — a versão de sessão exigiria unlock explícito, e um `kill -9`
deixaria a trava presa para sempre, com a automação parando em silêncio.

Detalhes de implementação e o passo a passo em
[docs/AUTOMACAO.md](docs/AUTOMACAO.md).

### O alerta dispara por igualdade, não por `<=`

```python
# app/domain/rules.py
def matched_alert_threshold(end_date, reference, thresholds) -> int | None:
    remaining = (end_date - reference).days
    return remaining if remaining in set(thresholds) else None
```

Com `<=`, um contrato a 20 dias do vencimento casaria com o limiar de 30 **todo
dia** até vencer: trinta e-mails para o mesmo cliente. Por igualdade, cada
limiar dispara uma vez.

---

## Integração com serviço externo

O `NotificationGateway` é uma porta do domínio. A implementação HTTP
(`app/infrastructure/gateways/http_notifier.py`) trata o que quebra de verdade
numa integração:

| Situação | Tratamento |
|---|---|
| Timeout | Separado por fase (conexão, leitura, escrita); retentável |
| Falha de conexão / DNS / TLS | Retentável; só a classe da exceção vai ao log, nunca a URL |
| `429`, `5xx` | Retentável, com espera exponencial **e jitter** |
| `400`, `401` | **Permanente** — não adianta repetir; sai na primeira |
| Provedor fora do ar | Disjuntor abre e recusa sem gastar timeout |
| Recuperação | Meia-abertura: uma sonda; sucesso fecha, falha reabre |
| Retry após entrega | `Idempotency-Key` faz o provedor descartar o duplicado |
| Resposta fora do contrato | Tolerada — não marca como falha um envio que deu certo |
| Esgotou as tentativas | Vai para `DEAD_LETTER`, com ação manual disponível |

O **jitter** não é refinamento: sem ele, 500 notificações que falharam no mesmo
segundo repetem juntas 0,5 s depois, e o provedor que estava se recuperando
leva a mesma rajada em sincronia e cai outra vez.

Para exercitar isso de verdade, o `docker-compose` sobe um **provedor
simulado** (`notifier_stub/`) que falha, demora e rejeita:

```bash
# força um comportamento específico
curl -X POST http://localhost:9090/v1/messages \
  -H "Authorization: Bearer $NOTIFIER_API_KEY" \
  -H "X-Stub-Behavior: timeout" \
  -H "Content-Type: application/json" \
  -d '{"channel":"email","to":"x@y.com","subject":"teste","body":"corpo"}'
```

---

## API

26 endpoints. Contrato completo — request, response, erros — em
[docs/API.md](docs/API.md), e interativo em `/docs`.

| Método | Rota | Permissão |
|---|---|---|
| `POST` | `/api/v1/auth/login` | pública (10/min por IP) |
| `POST` | `/api/v1/auth/refresh` | pública |
| `POST` | `/api/v1/auth/logout` | autenticado |
| `GET` | `/api/v1/auth/me` | autenticado |
| `POST` | `/api/v1/auth/change-password` | autenticado |
| `POST` | `/api/v1/auth/users` | `user:create` |
| `POST` `GET` | `/api/v1/clients` | `client:create` · `client:read` |
| `GET` `PATCH` | `/api/v1/clients/{id}` | `client:read` · `client:update` |
| `POST` | `/api/v1/clients/{id}/deactivate` | `client:deactivate` |
| `POST` `GET` | `/api/v1/contracts` | `contract:create` · `contract:read` |
| `GET` `PATCH` | `/api/v1/contracts/{id}` | `contract:read` · `contract:update` |
| `POST` | `/api/v1/contracts/{id}/cancel` | `contract:cancel` |
| `POST` | `/api/v1/contracts/{id}/renew` | `contract:renew` |
| `GET` | `/api/v1/contracts/{id}/history` | `contract:read` |
| `GET` | `/api/v1/contracts/{id}/installments` | `installment:read` |
| `POST` | `/api/v1/contracts/installments/{id}/settle` | `installment:settle` |
| `GET` | `/api/v1/notifications` | `notification:read` |
| `POST` | `/api/v1/notifications/{id}/retry` | `notification:retry` |
| `GET` | `/api/v1/jobs/runs` | `job:read` |
| `POST` | `/api/v1/jobs/daily-check/run` | `job:run` |
| `GET` | `/api/v1/audit/logs` | `audit:read` |
| `GET` | `/api/v1/audit/resources/{tipo}/{id}` | `audit:read` |
| `GET` | `/api/v1/admin/permissions` | `user:read` |
| `GET` | `/health` · `/health/live` · `/health/ready` | pública |

Todo erro sai em **RFC 7807** (`application/problem+json`):

```json
{
  "type": "https://sentinela.dev/errors/conflict",
  "title": "Conflito com o estado atual",
  "status": 409,
  "code": "conflict",
  "detail": "o contrato foi alterado por outra operação",
  "request_id": "9f3c1a7b2e4d5f60",
  "details": { "versao_atual": 3, "versao_enviada": 2 }
}
```

O `code` é estável e existe para o cliente programar em cima dele, em vez de
comparar a mensagem — que muda quando alguém corrige uma vírgula.

### Três sondas de saúde, com propósitos diferentes

`/health/live` não toca no banco de propósito. Se tocasse, uma queda do
PostgreSQL faria o orquestrador **reiniciar a aplicação** — remédio errado, com
efeito de nuvem de contêineres em laço de reinício enquanto o banco tenta se
recuperar. `/health/ready` verifica as dependências e tira a instância do
balanceador sem matá-la.

---

## Testes

```bash
pytest                                  # tudo
pytest tests/unit -q                    # 232, ~6s, sem banco
pytest -m integration                   # 91, exige PostgreSQL
pytest --cov=app --cov-report=term-missing
```

Os testes de integração exigem `TEST_DATABASE_URL`. **Sem a variável, eles são
ignorados com uma mensagem dizendo o que falta** — em vez de falharem com erro
de conexão. A diferença importa: teste vermelho por falta de infraestrutura
ensina o time a ignorar vermelho.

```bash
# um banco de teste descartável
docker compose up -d postgres
export TEST_DATABASE_URL="postgresql+psycopg://sentinela:SENHA@localhost:5432/sentinela_test"
pytest
```

### Como a suíte está dividida, e por quê

**`tests/unit/` — 232 testes, sem banco.** Regras de data e renovação,
dígito verificador de CPF/CNPJ, aritmética monetária em `Decimal`, política de
senha, todas as formas de rejeitar um JWT, a matriz de RBAC como *propriedade*,
janela deslizante sob concorrência real de threads, retry e disjuntor com
`respx`, e o ciclo de vida completo do contrato sobre repositórios em memória.

**`tests/integration/` — 91 testes, com PostgreSQL.** O que só o banco prova:
índice único, `ON CONFLICT`, gatilho de imutabilidade, trava otimista,
`ON DELETE`, além do formato real das respostas HTTP e dos cabeçalhos.

O schema dos testes vem das **migrations**, não de `metadata.create_all()`. A
diferença é decisiva: `create_all` pula tudo que só existe nas migrations — o
gatilho de imutabilidade, o índice único sobre `lower(email)` — e a suíte
passaria sem nunca exercitar as proteções mais importantes.

### Quatro defeitos reais que a construção encontrou

Ficam registrados porque são mais instrutivos que o resultado final:

1. **A auditoria saía sem autor.** `get_current_user` resolvia o usuário e
   chamava `set_context(...)`, e mesmo assim toda linha saía com `actor_email`
   nulo. Causa: endpoints `def` rodam no threadpool, e `anyio.to_thread.run_sync`
   **copia** o contexto por chamada — um `ContextVar.set()` numa dependência
   morre ali. Correção: o contexto é mutável, e a cópia compartilha o mesmo
   objeto. Sem erro, sem aviso, e uma trilha inútil.

2. **`rowcount` mentindo.** `add_if_absent` usava `bool(resultado.rowcount)`
   para saber se inseriu. Com psycopg3, `INSERT ... ON CONFLICT` devolve
   `rowcount` **-1** nos dois casos, e `bool(-1)` é `True`: o método respondia
   "inseri" sempre. O banco continuava recusando a duplicata, mas o job
   **relatava** alertas gerados a cada execução — o número do painel passou a
   mentir. Correção: `RETURNING id`. Passou por toda a suíte unitária, porque o
   dublê em memória implementava a semântica correta.

3. **Duas proteções incompatíveis.** `audit_logs.actor_user_id` tinha
   `ON DELETE SET NULL`, com a intenção certa. Só que o PostgreSQL implementa
   `SET NULL` como um `UPDATE` — que o gatilho de imutabilidade recusa. Remover
   um usuário era **impossível**, e a cláusula era código morto. Resolvido com
   `RESTRICT` e uma migration que explica a colisão: usuário é desativado, não
   apagado.

4. **Transação aberta durante chamadas de rede.** Este só apareceu com a pilha
   inteira de pé — nem os testes de integração pegaram. O despacho comitava uma
   vez no fim do lote, e cada envio faz uma chamada HTTP de segundos: a
   transação ficava ociosa durante toda a rede. Com 50 notificações e um
   provedor lento, passou de um minuto, e o PostgreSQL derrubou a conexão pelo
   `idle_in_transaction_session_timeout`. O sintoma —
   `server closed the connection unexpectedly` num INSERT de auditoria —
   parece problema de rede e é problema de desenho. O timeout ficou: foi ele
   que revelou o defeito. O commit passou a ser por notificação.

---

## Qualidade de código

```bash
# Windows
.\scripts\verificar.ps1
# Linux / macOS
./scripts/verificar.sh
```

O script roda os mesmos passos do CI: `ruff check`, `ruff format --check`,
`mypy app`, `pytest` e `alembic check`. Rodar localmente o que o CI roda evita
descobrir no push.

**Princípios aplicados**, com o exemplo concreto de cada um:

- **Responsabilidade única** — `AuditService` só audita; `ContractService` não
  sabe HTTP; `http_notifier` não sabe o que é um contrato.
- **Aberto/fechado** — trocar o provedor de notificação por uma fila ou por um
  webhook é escrever outra classe que satisfaça `NotificationGateway`. Nenhuma
  linha de `app/services/` muda.
- **Inversão de dependência** — as portas pertencem ao domínio; a
  infraestrutura se adapta. É o que permite `tests/unit/` rodar sem banco — e
  o `mypy` **prova** que o adaptador concreto satisfaz o `Protocol`, em vez de
  a conformidade ficar só na intenção.
- **Repository Pattern** — acesso a dados atrás de interface, com os dublês em
  `tests/fakes.py` como prova de que a abstração é real e não decorativa.
- **Unidade de trabalho** — um caso de uso é uma transação. "Criar contrato"
  grava contrato, parcelas, histórico e auditoria, ou não grava nada.
- **Injeção de dependência** — um container explícito, trocável por inteiro no
  teste sem tocar em nenhuma rota.

---

## Documentação complementar

| Documento | Conteúdo |
|---|---|
| [docs/ARQUITETURA.md](docs/ARQUITETURA.md) | Camadas, fluxo de execução e a justificativa de cada tecnologia |
| [docs/SEGURANCA.md](docs/SEGURANCA.md) | Modelo de ameaças, controle por controle, e o que **não** está coberto |
| [docs/BANCO-DE-DADOS.md](docs/BANCO-DE-DADOS.md) | Cada tabela, cada índice, cada `CHECK` e o porquê |
| [docs/AUTOMACAO.md](docs/AUTOMACAO.md) | Passo a passo do job, idempotência e exclusão mútua |
| [docs/API.md](docs/API.md) | Endpoints, exemplos de request/response e catálogo de erros |
| [docs/DECISOES.md](docs/DECISOES.md) | 12 ADRs com contexto, alternativas descartadas e reversibilidade |
| [docs/AMBIENTE-CORPORATIVO.md](docs/AMBIENTE-CORPORATIVO.md) | O que aproxima este projeto de um sistema real, e o que ainda falta |

---

## O que foi deixado de fora, e por quê

Cortes conscientes, não esquecimentos:

- **Rate limit distribuído.** O limitador é por processo. Com várias réplicas,
  o limite efetivo se multiplica. É aceitável porque a proteção real contra
  força bruta é o **bloqueio de conta**, que vive no banco e é compartilhado. A
  porta `RateLimiterPort` existe para que trocar por Redis seja escrever uma
  classe ([ADR-006](docs/DECISOES.md#adr-006)).
- **Revogação imediata de access token.** Um JWT vale até expirar; por isso o
  TTL é de 15 minutos. Revogar exigiria consultar uma lista de bloqueio a cada
  requisição, o que desfaz o motivo de usar JWT. O meio-termo adotado: o
  usuário é relido do banco a cada requisição, então desativar uma conta tem
  efeito imediato ([ADR-005](docs/DECISOES.md#adr-005)).
- **Migrations no entrypoint.** Funciona para uma réplica. Com várias, o certo
  é um Job de migração antes do deploy — está anotado no `entrypoint.sh`.
- **Métricas e rastreamento distribuído.** O log estruturado já carrega
  `request_id`, duração e status. Prometheus e OpenTelemetry seriam a próxima
  camada.
- **Multi-inquilino, anexos de contrato, juros e multa por atraso, e-mail em
  HTML.** Todos ampliam o domínio sem acrescentar nada ao que este projeto se
  propõe a demonstrar.

---

## Licença

MIT.
