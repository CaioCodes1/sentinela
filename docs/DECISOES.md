# Decisões de arquitetura (ADRs)

Cada registro traz o **contexto**, a **decisão**, as **alternativas
descartadas** com o motivo, e a **reversibilidade** — quanto custaria mudar de
ideia depois.

O formato existe porque a pergunta que aparece seis meses depois quase nunca é
"o que o código faz"; é "por que ele faz assim". Um comentário responde a
primeira; só um registro datado responde a segunda.

| # | Decisão | Status |
|---|---|---|
| [001](#adr-001) | SQLAlchemy síncrono com endpoints `def` | Aceita |
| [002](#adr-002) | Mapeamento imperativo, entidades puras | Aceita |
| [003](#adr-003) | Imutabilidade da auditoria por gatilho, não por `REVOKE` | Aceita |
| [004](#adr-004) | Refresh token rotativo com detecção de reúso | Aceita |
| [005](#adr-005) | Access token não é revogável; usuário relido a cada requisição | Aceita |
| [006](#adr-006) | Rate limit em memória, com porta para trocar | Aceita |
| [007](#adr-007) | Agendador no processo da API, com trava no banco | Aceita |
| [008](#adr-008) | Renovação cria contrato novo | Aceita |
| [009](#adr-009) | Idempotência no índice do banco, não no código | Aceita |
| [010](#adr-010) | RBAC centralizado em matriz única | Aceita |
| [011](#adr-011) | `RESTRICT` na auditoria em vez de `SET NULL` | Aceita, corrige a 003 |
| [012](#adr-012) | Migrations não importam código da aplicação | Aceita |

---

## ADR-001

### SQLAlchemy síncrono, com endpoints declarados como `def`

**Contexto.** FastAPI é async-native, e a maior parte do material disponível
mostra `async def` com SQLAlchemy assíncrono e asyncpg.

**Decisão.** Usar SQLAlchemy **síncrono** (psycopg 3) e declarar os endpoints
como `def`, não `async def`. O FastAPI executa handlers síncronos num
threadpool, sem bloquear o laço de eventos.

**Por quê.** O modo assíncrono só rende quando o gargalo é espera de E/S com
muita concorrência. O perfil desta aplicação é outro: consultas curtas por
chave primária e transações pequenas. O que o modo síncrono entrega em troca:

- **Semântica de transação mais simples.** A unidade de trabalho é um `with`
  comum, sem `async with` aninhado e sem risco de sessão compartilhada entre
  tarefas.
- **Nenhum bloqueio acidental.** O erro mais comum em FastAPI é declarar
  `async def` e chamar biblioteca síncrona dentro — o laço de eventos trava e a
  aplicação inteira fica lenta, com sintoma difuso. Com `def`, isso é
  impossível por construção.
- **O job agendado usa o mesmo código.** O APScheduler roda numa thread; sem
  `asyncio.run` ou ponte entre mundos.

**Alternativas descartadas.**

- *Async de ponta a ponta*: mais rápido sob alta concorrência de E/S, mais caro
  em complexidade e em formas novas de errar. Não se paga neste perfil de
  carga.
- *`async def` chamando ORM síncrono*: o pior dos dois — bloqueia o laço.

**Reversibilidade.** Média. Os repositórios teriam de virar `async`, e a
unidade de trabalho junto. O domínio e os serviços mudariam pouco, porque não
conhecem persistência. O sinal para mudar seria medido, não suposto: latência
dominada por espera de banco sob concorrência alta.

---

## ADR-002

### Mapeamento imperativo: entidades de domínio sem SQLAlchemy

**Contexto.** O caminho padrão é `DeclarativeBase`: a entidade herda de uma
classe do ORM e as colunas viram atributos de classe.

**Decisão.** Entidades são classes Python puras em `app/domain/entities.py`. A
ligação com as tabelas acontece em `app/infrastructure/db/mappers.py`, via
`registry.map_imperatively`.

**Por quê.** Com mapeamento declarativo, a regra de negócio passa a depender do
ORM estar configurado. Testar "contrato cancelado não pode ser renovado" exige
metadata carregada, e na prática exige banco. Aqui é `Contract(...)` e uma
chamada de método.

O resultado é mensurável: 274 dos 322 testes rodam em cerca de dois segundos,
sem Docker, sem migration e sem PostgreSQL. Uma suíte que roda rápido é uma
suíte que as pessoas rodam.

**Alternativas descartadas.**

- *Declarativo puro*: mais familiar, mas o domínio passa a herdar de
  infraestrutura — a seta de dependência aponta para o lado errado.
- *Entidades de domínio separadas dos modelos ORM, com mapeamento manual*:
  mantém a pureza e **dobra** o código, além de criar uma camada de tradução
  que é fonte própria de defeitos.

**Custo aceito.** Mapeamento imperativo é menos documentado; quem chega novo
precisa ler `mappers.py` para entender onde as entidades ganham persistência.
O arquivo existe e explica isso no topo.

**Reversibilidade.** Alta. Converter para declarativo é mecânico.

---

## ADR-003

### A imutabilidade da auditoria é garantida por gatilho, não por `REVOKE`

**Contexto.** A tabela `audit_logs` precisa ser somente-inserção.

**Decisão.** Um gatilho `BEFORE UPDATE OR DELETE` em PL/pgSQL que levanta
exceção. Sem `REVOKE` como mecanismo principal.

**Por quê.** `REVOKE UPDATE, DELETE` tem duas falhas conhecidas:

1. **Superusuário ignora permissão.** Em desenvolvimento e em teste a conexão
   costuma ser superusuário, então um teste de imutabilidade baseado em
   `REVOKE` passa verde **sem testar nada** — o pior tipo de teste, o que dá
   confiança falsa.
2. **O dono da tabela se reconcede.** `GRANT UPDATE ON audit_logs TO ...` é uma
   linha para quem tiver acesso ao banco.

O gatilho vale para superusuário, para o dono e para qualquer sessão.

**Porta de saída prevista.** `TRUNCATE` não dispara gatilho de linha. É a via
legítima para política de retenção e para limpeza entre testes, e foi pensada
junto com a proteção — não descoberta depois. Sem ela, a proteção de produção
inviabilizaria a suíte.

**Reversibilidade.** Alta: `DROP TRIGGER`. Mas reverter significa aceitar
trilha adulterável, o que esvazia o propósito da tabela.

---

## ADR-004

### Refresh token rotativo, com detecção de reúso por família

**Contexto.** Um refresh token de longa validade capturado dá acesso contínuo
ao atacante, e nada no sistema denuncia.

**Decisão.** Cada `/refresh` bem-sucedido revoga o token apresentado e emite
outro. Tokens nascidos do mesmo login compartilham um `family_id`. Se um token
**já revogado** for apresentado, a família inteira é revogada e o evento é
auditado como `TOKEN_REUSE_DETECTED`.

**Por quê.** Um token revogado reaparecendo tem duas explicações: ou o ladrão
está usando, ou o titular está usando depois do ladrão. Em ambos os casos há
cópia na mão errada. Derrubar a família força as duas partes a autenticar de
novo — o legítimo consegue, o ladrão não.

O token é guardado como **SHA-256**, nunca em claro: um dump do banco não pode
virar um conjunto de sessões válidas.

**Por que SHA-256 e não bcrypt.** O refresh token já é entropia gerada por nós,
não senha escolhida por humano; não há dicionário a resistir. Um bcrypt por
chamada de `/refresh` seria custo puro.

**Custo aceito.** Um logout inesperado quando duas abas renovam ao mesmo tempo.
É incômodo e barato perto do que evita.

**Reversibilidade.** Alta, e não vale a pena.

---

## ADR-005

### O access token não é revogável; a conta é verificada a cada requisição

**Contexto.** JWT é sem estado: uma vez assinado, vale até expirar. Revogar
exigiria consultar uma lista de bloqueio a cada requisição — o que elimina
exatamente a razão de usar JWT.

**Decisão.** Não manter lista de bloqueio de access token. Compensar com (a)
TTL curto, de 15 minutos, e (b) **releitura do usuário no banco a cada
requisição**, verificando `is_active` e lendo o papel dali.

**Por quê.** O risco concreto não é "o token continua válido por 15 minutos";
é "desativei o usuário e ele continua entrando". A releitura resolve isso com
uma busca por chave primária, que o PostgreSQL faz em microssegundos.

Ler o papel do banco e não do token também fecha o caso de rebaixamento: quem
foi de ADMIN para OPERATOR perde o privilégio na hora, não quando o token
expirar.

**Alternativas descartadas.**

- *Lista de bloqueio em Redis*: resolve, acrescenta um serviço e uma
  dependência de disponibilidade na autenticação.
- *TTL de 1 minuto*: multiplica as chamadas a `/refresh` sem ganho real.

**Reversibilidade.** Alta — a lista de bloqueio entraria como uma verificação a
mais em `get_current_user`.

---

## ADR-006

### Rate limit em memória, atrás de uma porta

**Contexto.** Proteger login contra força bruta e a API contra abuso.

**Decisão.** Janela deslizante em memória do processo, implementando
`RateLimiterPort`.

**Por quê janela deslizante e não janela fixa.** Janela fixa (contador zerado
na virada do minuto) tem uma brecha conhecida: com limite de 10/min, um cliente
manda 10 às 12:00:59 e mais 10 às 12:01:00 — 20 chamadas em um segundo, dentro
do limite pelas contas do algoritmo. Para força bruta de senha, essa é
exatamente a brecha que importa.

**Limitação assumida.** Com várias réplicas, cada uma conta sozinha e o limite
efetivo se multiplica. Isso é aceitável porque a proteção **real** contra força
bruta é o bloqueio de conta, que vive no banco e é compartilhado. O rate limit
é a primeira barreira, não a última.

**Por que a porta existe.** Trocar por Redis é escrever uma classe com dois
métodos. A porta é o que impede a alternativa comum: espalhar chamadas ao
limitador pelo código e depois ter de caçá-las todas.

**Reversibilidade.** Alta, por construção.

---

## ADR-007

### Agendador dentro do processo da API, com exclusão mútua no banco

**Contexto.** A verificação diária precisa rodar todo dia às 3h.

**Decisão.** APScheduler no próprio processo da API, com
`pg_try_advisory_xact_lock` garantindo que só uma instância execute.

**Por quê.** As alternativas custam uma peça de infraestrutura a mais para o
mesmo resultado:

- *Contêiner de cron separado*: outra imagem, outro deploy, outro lugar para a
  configuração divergir.
- *Cron do sistema operacional*: some em qualquer orquestrador moderno.
- *Celery beat*: exige broker; é a resposta certa para fila de trabalho, não
  para um job diário.

Com a trava no banco, rodar o agendador em toda réplica é seguro: a primeira a
chegar trabalha, as outras registram `SKIPPED` e saem.

**Por que a variante `xact` da trava.** Ela é liberada no fim da transação,
inclusive se o processo morrer. `pg_advisory_lock` (escopo de sessão) exigiria
unlock explícito, e um `kill -9` no meio do job deixaria a trava presa até
alguém reiniciar o banco — a automação pararia de rodar, em silêncio, para
sempre.

**Ajustes que evitam problemas conhecidos.** `coalesce=True` (três disparos
vencidos executam uma vez, não três), `misfire_grace_time` (execução muito
atrasada é descartada, em vez de mandar e-mail das 3h às 11h) e
`max_instances=1`.

**Reversibilidade.** Alta. `app/jobs/daily_job.py` é uma função pura de
entrada; qualquer agendador externo pode chamá-la.

---

## ADR-008

### Renovar cria um contrato novo; não estende o antigo

**Contexto.** Renovação poderia ser um `UPDATE` em `end_date`.

**Decisão.** A renovação cria um **contrato sucessor**. O original vai para
`RENEWED` apontando para o sucessor, e o sucessor aponta de volta.

**Por quê.** Esticar `end_date` apagaria o registro de que o período anterior
existiu naquele valor. O histórico do cliente passaria a mostrar um contrato de
cinco anos que nunca foi assinado assim — e as parcelas do período antigo
ficariam penduradas num contrato cujo período mudou.

Detalhe que decorre disso: o sucessor começa **no dia seguinte** ao término do
anterior. Começar no mesmo dia criaria um dia coberto por dois contratos — e
duas cobranças, que é o defeito que ninguém percebe até o cliente ligar.

O número preserva a linhagem: `CT-2026-001` → `CT-2026-001-R1` → `-R2`. Número
de contrato é lido por humano; um identificador novo sem relação com o anterior
quebraria a leitura.

**Reversibilidade.** Baixa depois de haver dados. Contratos sucessores já
criados não voltam a ser uma linha só.

---

## ADR-009

### A idempotência da automação mora no índice do banco

**Contexto.** O job precisa poder rodar duas vezes sem gerar cobrança ou aviso
em dobro — seja por reprocessamento manual, seja por duas réplicas.

**Decisão.** Cada notificação tem uma `dedupe_key` única
(`CONTRACT_EXPIRING:<contrato>:d30`) e cada parcela tem
`UNIQUE (contract_id, competence)`. A inserção usa
`INSERT ... ON CONFLICT DO NOTHING`.

**Por quê não verificar antes de inserir.** "Já existe?" seguido de `INSERT`
tem uma janela entre as duas operações. Duas réplicas passam as duas pela
verificação antes de qualquer uma inserir. O banco não tem essa janela.

**Por quê não `try/except IntegrityError`.** No PostgreSQL, **a primeira
instrução que falha aborta a transação inteira**. O `except` executa e o fluxo
continua, mas todo comando seguinte é recusado com
`current transaction is aborted`. O detalhe cruel é que o teste não pega: com
uma única duplicata e nada depois dela, o fluxo nunca chega ao comando
seguinte. Quebra em produção, no segundo item do lote.

**Detalhe que custou um defeito real.** Saber se a inserção aconteceu **não**
pode se apoiar em `rowcount`: com psycopg3, `INSERT ... ON CONFLICT` devolve
`-1` ("desconhecido") nos dois casos, e `bool(-1)` é `True`. O método respondia
"inseri" sempre, e as estatísticas do job passaram a mentir. A resposta correta
vem de `RETURNING id`.

**Reversibilidade.** Baixa, e não há motivo.

---

## ADR-010

### RBAC numa matriz central, nunca em `if` espalhado

**Contexto.** Autorização tende a apodrecer: começa com um `if user.role ==
ADMIN` num handler e termina impossível de auditar.

**Decisão.** Um mapa `papel -> conjunto de permissões` em
`app/core/permissions.py`. O endpoint declara a permissão que exige via
`Depends(require(Permission.X))`. Nenhum handler consulta `user.role`.

**Por quê.** Com a matriz central, "o que o Operador pode fazer?" tem resposta
num arquivo. Espalhada, a resposta muda a cada endpoint novo que alguém
esquece de proteger, e ninguém consegue enumerar.

Três coisas só existem por causa disso:

1. A matriz é testável como **propriedade** — "AUDITOR não tem nenhuma
   permissão de escrita" continua valendo quando alguém cria uma permissão
   nova.
2. `GET /api/v1/admin/permissions` publica a matriz viva, que não pode
   divergir do comportamento.
3. `test_toda_rota_exige_autorizacao` varre o app montado e reprova rota nova
   sem proteção.

**Decisão associada.** Não existe permissão de apagar auditoria. Se existisse,
bastaria promover alguém a ADMIN para apagar o próprio rastro. A imutabilidade
é do banco (ADR-003), não do RBAC.

**Reversibilidade.** Alta.

---

## ADR-011

### `ON DELETE RESTRICT` na auditoria, corrigindo o `SET NULL` original

*Corrige uma consequência não prevista do ADR-003.*

**Contexto.** `audit_logs.actor_user_id` foi criada com `ON DELETE SET NULL`,
com a intenção correta: apagar um usuário não pode levar junto o registro do
que ele fez.

**O problema.** O PostgreSQL implementa `SET NULL` como um `UPDATE` na tabela
referenciada — e `audit_logs` tem um gatilho que recusa `UPDATE`. Resultado:
`DELETE FROM users` falhava com o erro do gatilho. A remoção de usuário era
**impossível**, e a cláusula `ON DELETE SET NULL` era código morto: uma regra
escrita no schema que nunca poderia ser executada.

Isto não apareceria em teste unitário. Apareceu no primeiro teste de integração
que tentou remover um usuário.

**Decisão.** Trocar para `RESTRICT`, que diz a verdade sobre o sistema: usuário
não é apagado, é desativado — mesma política já aplicada a cliente.

**Alternativas descartadas.**

- *Afrouxar o gatilho para aceitar o `UPDATE` da chave estrangeira*: abriria a
  porta exata que a imutabilidade existe para fechar, e a exceção seria difícil
  de escrever sem virar brecha geral.
- *Deixar como estava*: manter uma regra que nunca dispara é pior que não ter
  regra, porque a próxima pessoa lê o schema e acredita nela.

**Nota geral.** Duas proteções desenhadas separadamente podem ser
incompatíveis, e o conflito costuma aparecer como "operação impossível", não
como erro de segurança. Vale conferir a interação sempre que se acrescenta um
gatilho a uma tabela com chave estrangeira.

**Reversibilidade.** Alta — a migration tem `downgrade`.

---

## ADR-012

### Migrations não importam código da aplicação

**Contexto.** O autogenerate do Alembic emite os tipos personalizados com o
caminho completo: `app.infrastructure.db.types.StrEnumType(length=40)`.

**Decisão.** Um `render_item` em `alembic/env.py` traduz os `TypeDecorator` para
o tipo SQL que eles de fato são: `sa.String(length=20)`, `sa.Numeric(14, 2)`.

**Por quê.** Três problemas com a migration importando código da aplicação:

1. Renomear ou apagar `StrEnumType` amanhã **quebra uma migration antiga** —
   que deveria ser um registro imutável do que já foi aplicado.
2. O comprimento renderizado vem do `impl` declarado na classe, não do tamanho
   passado na coluna: `role` sairia como `VARCHAR(40)` em vez de `VARCHAR(20)`.
3. Rodar migration deixa de ser possível sem o pacote da aplicação instalado, o
   que atrapalha em contêiner de migração enxuto.

**Decisão associada.** A URL do banco **não** fica no `alembic.ini`. Ela vem de
`Settings`, o mesmo objeto que a aplicação usa — evita versionar credencial e
evita que migration e aplicação apontem para bancos diferentes. O `env.py` só
preenche a URL quando ninguém já a definiu, para que a fixture de teste possa
apontar para o banco de teste. Sobrescrever incondicionalmente fazia as
migrations rodarem no banco de desenvolvimento enquanto os testes olhavam para
o banco de teste vazio, com sintoma de `relação "users" não existe`.

**Reversibilidade.** Alta.
