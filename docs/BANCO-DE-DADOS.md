# Banco de dados

PostgreSQL 16, nove tabelas, três migrations. Este documento explica cada
tabela, cada índice e cada restrição — **e o motivo de cada uma**.

O princípio que governa a modelagem: **validação em Pydantic protege a API;
`CHECK` protege o dado.** Um script de importação rodado às pressas, um `psql`
aberto em produção ou a próxima aplicação que apontar para este banco não passam
pelo Pydantic. Passam pelo `CHECK`.

---

## 1. Convenções gerais

| Convenção | Motivo |
|---|---|
| Chave primária **UUIDv4**, gerada na aplicação | Id sequencial em URL entrega quantos registros existem e convida a enumerar: `/contracts/1` → `/contracts/2`. Enumeração é a porta de entrada de IDOR |
| `TIMESTAMPTZ` em **toda** coluna de tempo | `TIMESTAMP` sem fuso é a origem do contrato que vence uma hora mais tarde entre outubro e fevereiro. Tudo gravado em UTC |
| `NUMERIC(14,2)` para dinheiro, **nunca** `float` | `SELECT 0.1::float8 + 0.2::float8` devolve `0.30000000000000004`. Somado em doze parcelas, é um centavo que ninguém explica para o financeiro |
| Enum como `VARCHAR` + `CHECK`, não `ENUM` nativo | Acrescentar valor a um tipo enum nativo é DDL com trava de tabela, e remover é praticamente impossível sem recriar o tipo. `VARCHAR` + `CHECK` dá a mesma proteção com migration de uma linha |
| `naming_convention` no `MetaData` | Sem ela, o PostgreSQL batiza as restrições sozinho e o `downgrade` tenta remover um `ck_contracts_1` que em outro ambiente virou `ck_contracts_2` |
| Índices **parciais** onde a consulta é parcial | Contrato encerrado é a maioria da tabela com o tempo e nunca entra na varredura diária |

---

## 2. `users`

Usuários do sistema. A senha existe aqui apenas como hash.

| Coluna | Tipo | Observação |
|---|---|---|
| `id` | `uuid` PK | |
| `email` | `varchar(254)` | Unicidade por `lower(email)` — ver abaixo |
| `password_hash` | `varchar(255)` | bcrypt |
| `full_name` | `varchar(120)` | |
| `role` | `varchar(20)` | `ADMIN`, `OPERATOR`, `AUDITOR` |
| `is_active` | `boolean` | Desativação, nunca remoção |
| `failed_login_attempts` | `integer` | Contador de força bruta |
| `locked_until` | `timestamptz` | Nulo quando não bloqueado |
| `last_login_at`, `password_changed_at` | `timestamptz` | |

**Restrições**

- `CHECK role IN ('ADMIN','OPERATOR','AUDITOR')`
- `CHECK failed_login_attempts >= 0`
- `CHECK length(password_hash) >= 20` — **impede senha em texto puro por
  construção.** Um hash bcrypt tem 60 caracteres; nenhuma senha plausível
  passaria por aqui sem ser notada, e a restrição pega o dia em que alguém
  "temporariamente" grava sem hashear.

**Índice funcional**

```sql
CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email));
```

Um `UNIQUE(email)` comum permitiria `Ana@x.com` e `ana@x.com` como duas contas.
Este índice é também o que o `get_by_email` usa: o repositório compara
`lower(email) = lower(:email)` justamente para casar com ele. Sem o índice
funcional, aquela consulta faria varredura sequencial na tabela de usuários **a
cada tentativa de login** — o endpoint que um atacante martela.

---

## 3. `refresh_tokens`

Sessões. Sustenta a rotação com detecção de reúso ([ADR-004](DECISOES.md#adr-004)).

| Coluna | Observação |
|---|---|
| `token_hash` | `varchar(64)` único — **SHA-256, nunca o token em claro** |
| `family_id` | Liga todos os tokens descendentes de um mesmo login |
| `expires_at`, `revoked_at`, `replaced_by_id` | Ciclo de vida |
| `created_ip` | `varchar(45)` — cabe IPv6 com mapeamento IPv4 |

`user_id` é `ON DELETE CASCADE`: token é dado derivado da conta; apagada a
conta, nenhuma sessão dela pode continuar valendo.

**Índice parcial**

```sql
CREATE INDEX ix_refresh_tokens_ativos ON refresh_tokens (expires_at)
    WHERE revoked_at IS NULL;
```

O `/refresh` só consulta tokens vivos. Com o tempo, revogados e expirados são a
maioria da tabela.

---

## 4. `clients`

| Coluna | Observação |
|---|---|
| `tax_id` | `varchar(14)` único — **só dígitos** |
| `legal_name`, `trade_name`, `email`, `phone` | |
| `status` | `ACTIVE` / `INACTIVE` |

**Por que o documento é guardado sem pontuação.** Guardar formatado permitiria o
mesmo CNPJ entrar duas vezes — `12.345.678/0001-95` e `12345678000195` — e
escapar da restrição de unicidade.

**Restrições**

- `CHECK length(tax_id) IN (11, 14)` — CPF ou CNPJ
- `CHECK tax_id ~ '^[0-9]+$'` — a normalização é garantida pelo banco, não pela
  esperança de que todo caminho de escrita lembre de normalizar

**Índice de trigramas**

```sql
CREATE INDEX ix_clients_legal_name_trgm ON clients USING gin (legal_name gin_trgm_ops);
```

`ILIKE '%termo%'` não usa índice B-tree: o curinga à esquerda impede.
A extensão é criada dentro de um bloco que tolera falta de permissão — em alguns
ambientes gerenciados o usuário da aplicação não pode criar extensão, e derrubar
a migration inteira por causa disso seria desproporcional.

---

## 5. `contracts`

O centro do domínio.

| Coluna | Observação |
|---|---|
| `number` | `varchar(40)` único; renovação gera `-R1`, `-R2` |
| `monthly_amount` | `NUMERIC(14,2)` |
| `start_date`, `end_date` | `date` |
| `due_day` | `1..31`, resolvido por mês na geração da parcela |
| `status` | `ACTIVE`, `EXPIRING`, `EXPIRED`, `RENEWED`, `CANCELLED` |
| `auto_renew`, `renewal_term_months` | Renovação automática |
| `renewed_to_id`, `previous_contract_id` | Cadeia navegável nos dois sentidos |
| `version` | **Trava otimista** |

**`client_id` é `ON DELETE RESTRICT`**, não `CASCADE`: apagar um cliente não pode
levar junto o histórico contratual dele. Cliente sai de cena por desativação.

**Restrições de faixa**

- `CHECK end_date > start_date`
- `CHECK monthly_amount > 0`
- `CHECK due_day BETWEEN 1 AND 31`
- `CHECK renewal_term_months BETWEEN 1 AND 120`

**Restrições de coerência** — as mais interessantes, porque pegam o defeito
silencioso:

```sql
CHECK ((status <> 'CANCELLED') OR (cancelled_at IS NOT NULL))
CHECK ((status <> 'RENEWED')   OR (renewed_to_id IS NOT NULL))
CHECK (renewed_to_id IS NULL OR renewed_to_id <> id)
```

Sem a primeira, existiria contrato `CANCELLED` sem data de cancelamento — e o
relatório de cancelamentos do mês deixaria de contá-lo, em silêncio. Sem a
segunda, um contrato `RENEWED` sem sucessor quebraria a cadeia. A terceira
impede um contrato apontar para si mesmo.

**O índice que sustenta a automação**

```sql
CREATE INDEX ix_contracts_vencimento_aberto ON contracts (end_date)
    WHERE status IN ('ACTIVE','EXPIRING');
```

A varredura diária filtra por status aberto e ordena por vencimento. Parcial
porque contrato encerrado nunca entra nessa consulta — e com o tempo eles são a
maioria da tabela.

**A trava otimista.** `version` começa em 1 e sobe a cada escrita. O SQLAlchemy
inclui `WHERE version = :lida` no `UPDATE`; se outra transação gravou no meio,
zero linhas são afetadas e o ORM levanta `StaleDataError`, que a unidade de
trabalho traduz em 409. Sem isso, duas edições simultâneas terminam com a última
sobrescrevendo a primeira em silêncio — e o operador que reajustou o valor
descobre semanas depois que a alteração dele sumiu.

---

## 6. `installments`

Mensalidades. Uma por competência.

| Coluna | Observação |
|---|---|
| `competence` | `varchar(7)`, formato `AAAA-MM` |
| `due_date` | Resolvido a partir de `due_day` e do mês |
| `amount` | `NUMERIC(14,2)` |
| `status` | `PENDING`, `PAID`, `OVERDUE`, `CANCELLED` |

**A restrição que torna a automação idempotente**

```sql
UNIQUE (contract_id, competence)
```

É esta restrição — e não a memória do processo — que garante que rodar o job
duas vezes não gera cobrança dupla. A inserção usa `ON CONFLICT DO NOTHING`
contra ela.

**Outras**

- `CHECK competence ~ '^[0-9]{4}-[0-9]{2}$'` — formato garantido no banco
- `CHECK amount > 0`
- `CHECK ((status <> 'PAID') OR (paid_at IS NOT NULL))`

**Índice parcial**

```sql
CREATE INDEX ix_installments_vencidas ON installments (due_date)
    WHERE status IN ('PENDING','OVERDUE');
```

**Sobre o dia de vencimento.** O contrato guarda o dia contratado (`due_day`) e
a data real é resolvida na geração de cada parcela: `min(due_day, último dia do
mês)`. Fevereiro não tem 31. Guardar 28 fixo quebraria março em diante;
resolver na hora preserva a intenção "no último dia do mês" em todos os meses.

---

## 7. `contract_events`

Histórico do contrato. **Somente inserção**, protegido pelo mesmo gatilho da
auditoria.

| Coluna | Observação |
|---|---|
| `event_type` | `CREATED`, `UPDATED`, `RENEWED`, `CANCELLED`, `EXPIRED`, `INSTALLMENT_SETTLED`, `CREATED_BY_RENEWAL` |
| `payload` | `jsonb` — em `UPDATED`, o **diff**: `{"monthly_amount": {"de": "1000.00", "para": "1200.00"}}` |
| `actor_user_id` | Quem fez |

"Contrato alterado" sem dizer o quê não responde a pergunta que alguém vai fazer
daqui a seis meses. Histórico alterável é histórico que não serve de prova — daí
o gatilho.

---

## 8. `notifications`

| Coluna | Observação |
|---|---|
| `dedupe_key` | `varchar(200)` **único** — a chave da idempotência |
| `notification_type`, `channel`, `recipient`, `subject`, `body` | |
| `status` | `PENDING`, `SENT`, `FAILED`, `DEAD_LETTER` |
| `attempts`, `last_error`, `provider_message_id`, `sent_at` | |

**`DEAD_LETTER` separado de `FAILED`** para o operador saber que o sistema não
vai tentar de novo sozinho — e que existe um botão para reenfileirar.

**A chave de deduplicação** tem a forma `TIPO:<contrato>:<marcador>`, e o
marcador é escolhido com cuidado:

| Tipo | Marcador | Por quê |
|---|---|---|
| `CONTRACT_EXPIRING` | o limiar (`d30`, `d7`) | Cada limiar dispara uma vez na vida do contrato. Se fosse a data, reprocessar um dia antigo criaria um aviso novo |
| `CONTRACT_EXPIRED` | `end_date` | O vencimento acontece uma única vez, naquela data |
| `INSTALLMENT_OVERDUE` | a competência | Uma cobrança por competência atrasada |

**Índice parcial do despacho**

```sql
CREATE INDEX ix_notifications_a_enviar ON notifications (created_at)
    WHERE status IN ('PENDING','FAILED');
```

A fila é consultada em ordem **crescente** de `created_at`. Com ordem
decrescente e um lote menor que a fila, as mais antigas nunca chegariam ao topo:
a cada execução entram novas na frente e as antigas envelhecem para sempre —
inanição de fila, com o painel mostrando "todas as execuções com sucesso" o
tempo inteiro.

---

## 9. `audit_logs`

A trilha. **Somente inserção, garantido por gatilho.**

| Coluna | Papel na investigação |
|---|---|
| `occurred_at` | Quando |
| `action`, `outcome` | O quê, e se deu certo |
| `resource_type`, `resource_id` | Sobre o quê |
| `actor_user_id` | Quem (referência) |
| `actor_email`, `actor_role` | Quem (**cópia do momento do ato**) |
| `ip_address`, `user_agent` | De onde |
| `request_id` | Amarra às entradas de log da mesma requisição |
| `details` | `jsonb` com o diff, o motivo, o contador |

**Por que o e-mail e o papel são copiados.** Se o usuário for renomeado ou
promovido depois, a trilha continua dizendo quem ele era naquele dia — que é o
ponto de auditoria.

**`actor_user_id` é `ON DELETE RESTRICT`.** A primeira versão usava `SET NULL`,
com a intenção certa. Ela é inexequível: o PostgreSQL implementa `SET NULL` como
um `UPDATE`, e o gatilho recusa `UPDATE`. Remover um usuário era impossível e a
cláusula era código morto. Ver [ADR-011](DECISOES.md#adr-011).

**Índices**

```sql
CREATE INDEX ix_audit_logs_occurred_at ON audit_logs (occurred_at DESC);
CREATE INDEX ix_audit_logs_actor_user_id_occurred_at ON audit_logs (actor_user_id, occurred_at DESC);
CREATE INDEX ix_audit_logs_resource_type_resource_id ON audit_logs (resource_type, resource_id);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);
```

`DESC` porque a pergunta do auditor é quase sempre "o que aconteceu por último";
sem isso, a ordenação vai para disco quando a tabela cresce.

### O gatilho

```sql
CREATE OR REPLACE FUNCTION recusar_alteracao_de_registro_imutavel()
RETURNS TRIGGER LANGUAGE plpgsql
SECURITY INVOKER SET search_path = pg_catalog, public
AS $$
BEGIN
    RAISE EXCEPTION 'a tabela % é somente-inserção: % não é permitido (registro %)',
        TG_TABLE_NAME, TG_OP, COALESCE(OLD.id::text, '?')
        USING ERRCODE = 'restrict_violation',
              HINT = 'Trilhas de auditoria e histórico não podem ser alteradas nem removidas.';
END;
$$;
```

Aplicado a `audit_logs` e `contract_events` como `BEFORE UPDATE OR DELETE ... FOR
EACH ROW`.

O `search_path` fixo é a recomendação padrão para função em PL/pgSQL: sem ele,
um schema malicioso no caminho de busca poderia sombrear uma função usada
internamente.

**A porta de saída.** `TRUNCATE` não dispara gatilho de linha. É o caminho
previsto para política de retenção e para limpeza entre testes — pensado junto
com a proteção, não descoberto depois.

---

## 10. `job_runs`

Execuções da automação.

| Coluna | Observação |
|---|---|
| `job_name`, `status` | `RUNNING`, `SUCCESS`, `FAILED`, `SKIPPED` |
| `started_at`, `finished_at` | `CHECK finished_at >= started_at` |
| `stats` | `jsonb` com o que foi feito |
| `error_message` | |

Sem esta tabela, "o job rodou hoje?" não tem resposta. A única evidência seria o
log — que expira, que pode não ter sido coletado naquela noite, e que ninguém
consulta até o cliente reclamar de um aviso que não chegou.

`SKIPPED` registra a execução que encontrou a trava tomada. Não é erro: é a
segunda réplica se comportando corretamente.

---

## 11. Migrations

| Revisão | Conteúdo |
|---|---|
| `c77c6a96d280` | Schema inicial — todas as tabelas, restrições e índices declaráveis |
| `b2a1f0c3d4e5` | Gatilho de imutabilidade, índice funcional de e-mail, índice de trigramas, comentários |
| `c9d8e7f6a5b4` | `audit_logs.actor_user_id` de `SET NULL` para `RESTRICT` |

A segunda é escrita à mão, porque o autogenerate não enxerga gatilho nem função.
A terceira corrige a colisão descrita no [ADR-011](DECISOES.md#adr-011).

### `alembic check` como portão de deriva

```bash
alembic check   # "No new upgrade operations detected."
```

Funciona como portão de verdade neste projeto porque os índices funcionais e os
comentários estão declarados **no modelo**, e não só nas migrations. Um índice
que existisse apenas na migration apareceria como "removido" a cada execução, e
o portão viraria ruído que todo mundo aprende a ignorar.

### Migrations não importam código da aplicação

Um `render_item` em `alembic/env.py` traduz os `TypeDecorator` para o tipo SQL
correspondente. Ver [ADR-012](DECISOES.md#adr-012).

---

## 12. Consultas de operação

```sql
-- contratos a vencer nos próximos 30 dias
SELECT c.number, cl.legal_name, c.end_date, c.monthly_amount,
       c.end_date - CURRENT_DATE AS dias_restantes
FROM contracts c
JOIN clients cl ON cl.id = c.client_id
WHERE c.status IN ('ACTIVE','EXPIRING')
  AND c.end_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 30
ORDER BY c.end_date;

-- inadimplência por cliente
SELECT cl.legal_name,
       count(*) AS parcelas_em_atraso,
       sum(i.amount) AS total
FROM installments i
JOIN contracts c ON c.id = i.contract_id
JOIN clients cl ON cl.id = c.client_id
WHERE i.status = 'OVERDUE'
GROUP BY cl.legal_name
HAVING sum(i.amount) > 0
ORDER BY total DESC;

-- trilha completa de um contrato
SELECT occurred_at, action, actor_email, actor_role, ip_address, details
FROM audit_logs
WHERE resource_type = 'contract' AND resource_id = :id
ORDER BY occurred_at DESC;

-- notificações que precisam de ação humana
SELECT n.created_at, c.number, n.notification_type, n.attempts, n.last_error
FROM notifications n
JOIN contracts c ON c.id = n.contract_id
WHERE n.status = 'DEAD_LETTER'
ORDER BY n.created_at;

-- a automação rodou? o que ela fez?
SELECT started_at, status,
       extract(epoch FROM (finished_at - started_at)) AS duracao_s,
       stats
FROM job_runs
WHERE job_name = 'verificacao-diaria-contratos'
ORDER BY started_at DESC
LIMIT 10;
```
