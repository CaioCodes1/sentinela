# API

Base: `/api/v1` · Documentação interativa: `/docs` · Especificação:
`/openapi.json`

Todo endpoint, exceto `/auth/login`, `/auth/refresh` e as sondas de saúde, exige
`Authorization: Bearer <access_token>`.

---

## 1. Convenções

### Respostas de erro — RFC 7807

Toda falha sai como `application/problem+json`:

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

O `code` é **estável** e existe para o cliente programar em cima dele, em vez de
comparar a mensagem — que muda quando alguém corrige uma vírgula.

O `request_id` aparece também no cabeçalho `X-Request-Id` e nas entradas de log
do servidor. É o que liga a resposta (deliberadamente opaca) ao registro que tem
o detalhe real.

### Catálogo de códigos

| `code` | HTTP | Quando |
|---|---|---|
| `schema_validation_error` | 422 | Corpo não passou na validação do schema |
| `validation_error` | 400 | Valor inválido detectado no domínio |
| `authentication_failed` | 401 | Credencial ausente, inválida ou expirada |
| `account_locked` | 423 | Conta bloqueada por excesso de tentativas |
| `not_authorized` | 403 | Perfil sem a permissão exigida |
| `not_found` | 404 | Recurso inexistente |
| `conflict` | 409 | Documento/número duplicado, ou versão divergente |
| `concurrent_modification` | 409 | Trava otimista disparou |
| `business_rule_violation` | 422 | Operação proibida pelo estado atual |
| `rate_limited` | 429 | Limite excedido (com `Retry-After`) |
| `payload_too_large` | 413 | Corpo acima de 256 KB |
| `external_service_unavailable` | 502 | Provedor de notificação indisponível |
| `database_unavailable` | 503 | Banco indisponível |
| `internal_error` | 500 | Falha não prevista |

### Paginação

Listagens aceitam `limit` (1–200, padrão 50) e `offset`, e devolvem:

```json
{ "items": [ ... ], "total": 137, "limit": 50, "offset": 0 }
```

O `total` vem junto porque paginação sem total força o cliente a adivinhar se
existe próxima página.

### Cabeçalhos de resposta

`X-Request-Id`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, mais os cabeçalhos
de segurança descritos em [SEGURANCA.md](SEGURANCA.md#8-cabeçalhos-http).

---

## 2. Autenticação

### `POST /api/v1/auth/login`

Público. Limite de **10 por minuto por IP**.

```json
{ "email": "admin@sentinela.local", "password": "SenhaForte#2026" }
```

**200**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 900,
  "role": "ADMIN"
}
```

| Erro | Situação |
|---|---|
| `401 authentication_failed` | E-mail inexistente, senha errada **ou** conta desativada — sempre a mesma mensagem |
| `423 account_locked` | 5 tentativas erradas; bloqueio de 15 minutos |
| `429 rate_limited` | Mais de 10 tentativas no minuto |

> A mensagem única e o tempo de resposta constante são deliberados: mensagens
> distintas transformariam o login num verificador de quais e-mails estão
> cadastrados.

### `POST /api/v1/auth/refresh`

```json
{ "refresh_token": "eyJhbGciOiJIUzI1NiIs..." }
```

Devolve um par novo e **invalida o token apresentado**. Apresentar de novo um
token já usado é tratado como indício de roubo: toda a família daquele login é
revogada e o evento vai para a auditoria.

| Erro | Situação |
|---|---|
| `401` "token inválido" | Assinatura errada, tipo errado, ou token desconhecido |
| `401` "token expirado" | Passou dos 7 dias |
| `401` "sessão encerrada por motivo de segurança" | **Reúso detectado** |

### `POST /api/v1/auth/logout`

```json
{ "refresh_token": "...", "all_sessions": false }
```

`200` → `{"message": "1 sessão(ões) revogada(s)"}`

Com `all_sessions: true`, derruba todas. O access token continua válido até
expirar (no máximo 15 minutos) — ver [ADR-005](DECISOES.md#adr-005).

### `GET /api/v1/auth/me`

```json
{
  "id": "0d1b...",
  "email": "operador@sentinela.local",
  "full_name": "Operador",
  "role": "OPERATOR",
  "is_active": true,
  "last_login_at": "2026-09-17T14:02:11Z",
  "created_at": "2026-09-01T09:00:00Z",
  "permissions": ["client:create", "client:read", "contract:create", "..."]
}
```

As permissões servem à **interface**, para decidir o que mostrar. A autorização
de verdade acontece no servidor, em toda rota: uma interface que esconde o botão
não protege nada se o endpoint estiver aberto.

### `POST /api/v1/auth/change-password`

```json
{ "current_password": "...", "new_password": "NovaChave#2026Forte" }
```

Exige a senha atual e **encerra todas as sessões**.

### `POST /api/v1/auth/users` — `user:create`

```json
{
  "email": "auditor@empresa.com",
  "password": "ChaveForte#2026",
  "full_name": "Auditoria Interna",
  "role": "AUDITOR"
}
```

`201`. O primeiro administrador **não** nasce aqui — nasce de
`python -m app.cli criar-admin`. Uma rota pública de inicialização, esquecida
aberta, é uma porta para criar contas de administrador.

---

## 3. Clientes

### `POST /api/v1/clients` — `client:create`

```json
{
  "tax_id": "12.345.678/0001-95",
  "legal_name": "Aurora Serviços Financeiros Ltda",
  "trade_name": "Aurora Financeira",
  "email": "financeiro@aurora.com.br",
  "phone": "1133334444"
}
```

**201**

```json
{
  "id": "8a2f...",
  "tax_id": "12.345.678/0001-95",
  "legal_name": "Aurora Serviços Financeiros Ltda",
  "trade_name": "Aurora Financeira",
  "email": "financeiro@aurora.com.br",
  "phone": "1133334444",
  "status": "ACTIVE",
  "created_at": "2026-09-17T14:10:00Z",
  "updated_at": "2026-09-17T14:10:00Z"
}
```

O documento é aceito com ou sem pontuação, validado pelo dígito verificador e
guardado só com dígitos — o que faz a unicidade funcionar de verdade.

| Erro | Situação |
|---|---|
| `422` | Documento com dígito verificador inválido, e-mail malformado, **ou campo desconhecido no corpo** |
| `409 conflict` | Documento já cadastrado |

### `GET /api/v1/clients` — `client:read`

`?term=aurora` · `?only_active=true` · `?limit=50&offset=0`

O `term` busca por razão social, nome fantasia ou prefixo do documento. Os
curingas do `LIKE` são escapados: buscar por `%` devolve zero, não a base
inteira.

### `GET /api/v1/clients/{id}` · `PATCH /api/v1/clients/{id}`

O `PATCH` aceita `legal_name`, `trade_name`, `email` e `phone`. **O documento
não é alterável**: trocar o CNPJ não é editar um cadastro, é apontar todos os
contratos históricos para outra pessoa jurídica.

### `POST /api/v1/clients/{id}/deactivate` — `client:deactivate`

```json
{ "reason": "encerramento de relacionamento" }
```

É `POST /deactivate` e não `DELETE` de propósito: o verbo descreve o que de fato
acontece. Ninguém deve descobrir lendo o log que o "delete" desta API na verdade
preserva tudo.

`422` se houver contrato vigente — desativar o titular de um contrato em vigor
deixaria a cobrança rodando para um cadastro inativo:

```json
{ "code": "business_rule_violation", "details": { "contratos_vigentes": 2 } }
```

---

## 4. Contratos

### `POST /api/v1/contracts` — `contract:create`

```json
{
  "client_id": "8a2f...",
  "number": "CT-2026-001",
  "description": "Prestação de serviços de consultoria",
  "monthly_amount": "1500.00",
  "start_date": "2026-01-01",
  "end_date": "2026-12-31",
  "due_day": 10,
  "auto_renew": true,
  "renewal_term_months": 12
}
```

**201** — e, na mesma transação, as 12 mensalidades são geradas. Se a geração
falhar, o contrato não é criado: a alternativa produziria contratos sem
cobrança, que só aparecem quando o financeiro fecha o mês.

`monthly_amount` vai e volta como **string decimal**, para não passar por
`float` em nenhum ponto do caminho.

### `GET /api/v1/contracts` — `contract:read`

`?client_id=...` · `?status=ACTIVE` · `?expiring_until=2026-10-17`

O filtro `expiring_until` é o que uma tela de "a vencer" consome.

### `PATCH /api/v1/contracts/{id}` — `contract:update`

```json
{ "monthly_amount": "1650.00", "expected_version": 3 }
```

`start_date`, `client_id` e `number` não são alteráveis: os três definem a
identidade do contrato.

**Trava otimista.** Envie `expected_version` com a versão que você leu. Se outro
operador gravou nesse intervalo:

```json
{
  "status": 409,
  "code": "conflict",
  "detail": "o contrato foi alterado por outra operação",
  "details": { "versao_atual": 4, "versao_enviada": 3 }
}
```

O campo é opcional: um cliente que não precisa controlar concorrência não é
obrigado a enviá-lo.

Prorrogar `end_date` gera as parcelas das competências novas, sem duplicar as
existentes — e sem tocar em parcela já paga.

### `POST /api/v1/contracts/{id}/cancel` — `contract:cancel`

```json
{ "reason": "rescisão solicitada pelo cliente" }
```

Terminal: contrato cancelado não é editado, nem renovado, nem reaberto. O motivo
é obrigatório e entra no histórico.

### `POST /api/v1/contracts/{id}/renew` — `contract:renew`

```json
{ "term_months": 12, "adjustment_percent": "8.5" }
```

**201**, devolvendo o **contrato novo** — não o antigo. A renovação cria um
sucessor com vigência começando no dia seguinte ao término do anterior; o
original vai para `RENEWED` apontando para ele. Por isso 201 e não 200: um
recurso novo foi criado.

O número preserva a linhagem: `CT-2026-001` → `CT-2026-001-R1` → `-R2`.

### `GET /api/v1/contracts/{id}/history` — `contract:read`

```json
[
  {
    "event_type": "UPDATED",
    "payload": {
      "alteracoes": { "monthly_amount": { "de": "1500.00", "para": "1650.00" } }
    },
    "actor_user_id": "0d1b...",
    "occurred_at": "2026-09-17T14:22:03Z"
  },
  { "event_type": "CREATED", "payload": { "...": "..." } }
]
```

Mais recente primeiro. O histórico é **somente inserção**, garantido por gatilho
no banco.

### `GET /api/v1/contracts/{id}/installments` — `installment:read`

```json
[
  { "competence": "2026-01", "due_date": "2026-01-10", "amount": "1500.00", "status": "PAID",    "paid_at": "2026-01-08T11:00:00Z" },
  { "competence": "2026-02", "due_date": "2026-02-10", "amount": "1500.00", "status": "OVERDUE", "paid_at": null }
]
```

### `POST /api/v1/contracts/installments/{id}/settle` — `installment:settle`

Registra a quitação. `422` se a parcela já estiver paga ou cancelada.

---

## 5. Operação

### `GET /api/v1/notifications` — `notification:read`

`?status=DEAD_LETTER` · `?contract_id=...`

```json
{
  "items": [
    {
      "id": "3f1a...",
      "contract_id": "7c2e...",
      "notification_type": "CONTRACT_EXPIRING",
      "channel": "email",
      "recipient": "f***@aurora.com.br",
      "subject": "Contrato CT-2026-001 vence em 30 dia(s)",
      "status": "SENT",
      "attempts": 1,
      "last_error": null,
      "provider_message_id": "msg_9a8b7c6d",
      "sent_at": "2026-09-17T03:00:02Z"
    }
  ],
  "total": 1, "limit": 50, "offset": 0
}
```

O destinatário sai **mascarado**: quem consulta o painel precisa saber se o
aviso saiu, não colher a lista de e-mails dos clientes.

Filtre por `status=DEAD_LETTER` para achar o que esgotou as tentativas e precisa
de ação humana.

### `POST /api/v1/notifications/{id}/retry` — `notification:retry`

Recoloca na fila e zera o contador de tentativas. `422` se já foi enviada.

O histórico das tentativas anteriores não se perde: cada uma gerou uma linha de
auditoria.

### `GET /api/v1/jobs/runs` — `job:read`

```json
[
  {
    "job_name": "verificacao-diaria-contratos",
    "status": "SUCCESS",
    "started_at": "2026-09-17T03:00:00Z",
    "finished_at": "2026-09-17T03:00:04Z",
    "duration_seconds": 4.2,
    "stats": {
      "contratos_analisados": 1284,
      "alertas_gerados": 17,
      "contratos_expirados": 3,
      "envio": { "processadas": 20, "enviadas": 19, "falhas": 1 }
    },
    "error_message": null
  }
]
```

### `POST /api/v1/jobs/daily-check/run` — `job:run`

```json
{ "reference": "2026-09-16" }
```

`reference` é opcional; o padrão é hoje. Restrito ao ADMIN porque **envia
mensagem real**. É idempotente: reprocessar um dia não gera aviso duplicado.

Se outra instância já estiver executando, devolve `{"status": "SKIPPED"}` —
comportamento correto, não erro.

### `GET /api/v1/admin/permissions` — `user:read`

```json
{
  "ADMIN":    ["audit:read", "client:create", "..."],
  "OPERATOR": ["client:create", "client:read", "..."],
  "AUDITOR":  ["audit:read", "client:read", "..."]
}
```

A matriz **viva**, lida do código em tempo de execução. Um documento descrevendo
permissões envelhece no primeiro endpoint novo; este endpoint não tem como
divergir.

---

## 6. Auditoria

### `GET /api/v1/audit/logs` — `audit:read`

`?actor_user_id=` · `?action=LOGIN_FAILURE` · `?resource_type=contract` ·
`?resource_id=` · `?occurred_from=` · `?occurred_to=`

```json
{
  "items": [
    {
      "id": "b7c2...",
      "occurred_at": "2026-09-17T14:22:03Z",
      "action": "CONTRACT_UPDATED",
      "outcome": "SUCCESS",
      "resource_type": "contract",
      "resource_id": "7c2e...",
      "actor_user_id": "0d1b...",
      "actor_email": "operador@sentinela.local",
      "actor_role": "OPERATOR",
      "ip_address": "10.0.0.14",
      "request_id": "9f3c1a7b2e4d5f60",
      "details": {
        "alteracoes": { "monthly_amount": { "de": "1500.00", "para": "1650.00" } },
        "versao": 4
      }
    }
  ],
  "total": 218, "limit": 50, "offset": 0
}
```

### `GET /api/v1/audit/resources/{tipo}/{id}` — `audit:read`

Atalho para "tudo o que já aconteceu com este contrato/cliente".

Não existe rota de alteração nem de remoção: a tabela é protegida por gatilho, e
expor um endpoint que o banco vai recusar seria pior que não ter endpoint.

---

## 7. Saúde

| Rota | Toca o banco? | Para quê |
|---|:---:|---|
| `GET /health/live` | **não** | *Liveness*: o processo responde? |
| `GET /health/ready` | sim | *Readiness*: dá para receber tráfego? |
| `GET /health` | não | Resumo legível |

`/health/live` não toca no banco de propósito. Se tocasse, uma queda do
PostgreSQL faria o orquestrador **reiniciar a aplicação** — remédio errado, com
efeito de nuvem de contêineres em laço de reinício enquanto o banco se recupera.

```json
{
  "status": "ready",
  "checks": { "database": "ok", "notification_provider": "indisponível" }
}
```

O provedor de notificação **não** entra na decisão de prontidão: ele é
assíncrono por natureza, a fila absorve a indisponibilidade e reenvia depois.
Tirar a API do ar porque o serviço de e-mail caiu seria deixar de atender
contrato e cliente por causa de um aviso que pode esperar.

---

## 8. Exemplo completo

```bash
BASE=http://localhost:8000/api/v1

TOKEN=$(curl -s -X POST $BASE/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@sentinela.local","password":"SUA_SENHA"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

AUTH="Authorization: Bearer $TOKEN"
JSON="Content-Type: application/json"

CLIENTE=$(curl -s -X POST $BASE/clients -H "$AUTH" -H "$JSON" -d '{
  "tax_id": "12.345.678/0001-95",
  "legal_name": "Aurora Serviços Financeiros Ltda",
  "email": "financeiro@aurora.com.br"
}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

CONTRATO=$(curl -s -X POST $BASE/contracts -H "$AUTH" -H "$JSON" -d "{
  \"client_id\": \"$CLIENTE\",
  \"number\": \"CT-2026-001\",
  \"monthly_amount\": \"1500.00\",
  \"start_date\": \"2026-01-01\",
  \"end_date\": \"2026-12-31\",
  \"due_day\": 10
}" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -s -H "$AUTH" $BASE/contracts/$CONTRATO/installments

curl -s -X POST $BASE/contracts/$CONTRATO/renew -H "$AUTH" -H "$JSON" \
  -d '{"term_months": 12, "adjustment_percent": "8.5"}'

curl -s -X POST $BASE/jobs/daily-check/run -H "$AUTH" -H "$JSON" -d '{}'

curl -s -H "$AUTH" "$BASE/audit/resources/contract/$CONTRATO"
```
