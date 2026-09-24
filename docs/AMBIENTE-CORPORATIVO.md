# O que aproxima este projeto de um sistema corporativo real

Projeto de portfólio costuma parar no CRUD que funciona. O que diferencia um
sistema que roda numa empresa não é a lista de funcionalidades — é o que
acontece **fora do caminho feliz**, e o que sobra para investigar quando algo dá
errado às 3h da manhã.

Este documento lista o que foi implementado com esse critério, e é honesto sobre
o que ainda falta.

---

## 1. Sete perguntas que um sistema corporativo precisa responder

### "Quem alterou isso?"

A pergunta mais cara de não conseguir responder. Aqui ela é respondida por
`GET /api/v1/audit/resources/contract/{id}`, que devolve quem, o quê, quando, de
onde e o **diff** de cada alteração.

A trilha é imutável por gatilho no banco, então nem quem tem acesso ao `psql`
consegue reescrevê-la. E o e-mail e o papel do autor são cópias do momento do
ato: se a pessoa for promovida ou renomeada depois, a trilha continua dizendo
quem ela era naquele dia.

### "O job rodou ontem?"

`GET /api/v1/jobs/runs` devolve as últimas execuções com duração, status e
estatísticas. Sem a tabela `job_runs`, a única evidência seria o log — que
expira, que pode não ter sido coletado naquela noite, e que ninguém consulta até
o cliente reclamar de um aviso que não chegou.

O registro inclui as execuções `SKIPPED`, que são a segunda réplica se
comportando corretamente.

### "Por que este cliente recebeu dois e-mails?"

Ele não recebeu. Cada notificação tem uma chave única no banco, e o alerta
dispara por igualdade de limiar, não por `<=`. Isso está coberto por teste, e o
`provider_message_id` registrado permite cruzar com o log do provedor numa
investigação.

### "O que aconteceu na requisição que falhou?"

Toda resposta de erro traz um `request_id`, que aparece também no cabeçalho
`X-Request-Id` e em toda entrada de log daquela requisição. O suporte pede o id
ao cliente e encontra a linha exata no agregador — sem que a resposta de erro
tenha contado nada ao mundo.

### "Podemos reprocessar o dia 16?"

Sim, com um comando, e sem efeito colateral:
`POST /api/v1/jobs/daily-check/run {"reference": "2026-09-16"}`. A idempotência
é garantida por índice no banco, não por convenção.

Reprocessamento seguro é a diferença entre "vamos investigar amanhã" e "vamos
rodar de novo e ver".

### "O que este perfil pode fazer?"

`GET /api/v1/admin/permissions` devolve a matriz **viva**, lida do código em
tempo de execução. Documento de permissões envelhece no primeiro endpoint novo;
este endpoint não tem como divergir do comportamento.

### "O sistema está saudável?"

Três sondas com propósitos distintos, e a distinção importa: `/health/live` não
toca no banco, porque se tocasse uma queda do PostgreSQL faria o orquestrador
reiniciar a aplicação — remédio errado, que produz uma nuvem de contêineres em
laço de reinício enquanto o banco tenta se recuperar.

---

## 2. Decisões que só aparecem em sistema que já sofreu

| Decisão | O problema real que ela evita |
|---|---|
| `NUMERIC(14,2)`, nunca `float` | Centavo perdido depois de doze parcelas somadas, impossível de explicar para o financeiro |
| `TIMESTAMPTZ` em tudo | Contrato que vence uma hora mais tarde entre outubro e fevereiro |
| Renovação cria contrato novo | Histórico do cliente mostrando um contrato de cinco anos que nunca foi assinado assim |
| Sucessor começa no dia **seguinte** | Um dia coberto por dois contratos — e duas cobranças |
| Vencido só a partir do dia seguinte ao término | Cobertura encerrada um dia antes da hora |
| Atraso só a partir do dia seguinte ao vencimento | Cobrança de inadimplência para quem tem o dia inteiro para pagar |
| `due_day` resolvido por mês | Fevereiro não tem 31; gravar 28 fixo quebra março em diante |
| Alerta por igualdade de limiar | Trinta e-mails para o mesmo cliente |
| Fila consumida em ordem crescente | Inanição: as mais antigas nunca saem, com o painel 100% verde |
| Contrato cancelado não gera cobrança | "Cancelei e continuam me cobrando" |
| Trava otimista nos contratos | Alteração de um operador sumindo em silêncio |
| `ON DELETE RESTRICT` no cliente | Apagar o cliente levando junto o histórico contratual |
| Desativar em vez de apagar | "Quem era o titular deste contrato de 2023?" |
| Índices parciais | Varredura diária degradando conforme a base de contratos encerrados cresce |
| `CHECK` de coerência status × colunas | Relatório de cancelamentos que deixa de contar um contrato, em silêncio |
| `pool_pre_ping` | Primeira requisição de cada conexão falhando toda vez que o banco reinicia |
| `statement_timeout` | Uma consulta patológica segurando o pool inteiro |

Nenhuma delas é difícil. Todas são invisíveis até o dia em que custam caro.

---

## 3. Operação

**Configuração por ambiente**, sem valor embutido. Em produção a aplicação
recusa subir com segredo de exemplo, com `BCRYPT_ROUNDS` abaixo de 12, com
`DEBUG` ligado ou com `CORS_ORIGINS: *`. Falhar no boot é melhor que descobrir
em auditoria.

**Migrations versionadas**, com `downgrade` e um portão de deriva (`alembic
check`) que reprova o build se o modelo e o banco discordarem.

**Log estruturado em JSON**, com `request_id`, ator, IP, método, caminho
(template, não o id — cardinalidade), status e duração. Pronto para agregador,
com redação de dado pessoal aplicada a todo registro.

**Contêiner endurecido**: usuário sem privilégios, sistema de arquivos em
somente-leitura, `cap_drop: ALL`, `no-new-privileges`, limite de memória, portas
publicadas apenas no laço local.

**Encerramento ordenado**: o agendador para antes de o pool de conexões fechar,
para que um job disparado no último segundo não registre uma falha que não é
falha.

**Pipeline com portões de verdade**: lint, formato, tipos, testes com banco
real, deriva de schema, varredura de dependências e de imagem. O portão do
Trivy é montado em duas etapas de propósito — a configuração ingênua faz o build
reprovar por CVE *medium* e leva a "consertar" baixando o `severity`, o que
transforma o portão em enfeite.

---

## 4. A suíte de testes

274 testes unitários em ~2 segundos, 48 de integração com PostgreSQL real.

Duas características que valem mais que o número:

**Os testes de integração são ignorados, não falham, quando não há banco.** Com
uma mensagem dizendo o que falta. Teste vermelho por falta de infraestrutura
ensina o time a ignorar vermelho.

**O schema dos testes vem das migrations**, não de `metadata.create_all()`.
`create_all` pula tudo que só existe nas migrations — o gatilho de imutabilidade,
o índice único funcional — e a suíte passaria sem nunca exercitar as proteções
mais importantes do banco. De quebra, rodar as migrations testa as migrations.

E três defeitos reais que a suíte encontrou estão documentados no
[README](../README.md#três-defeitos-reais-que-a-suíte-encontrou), porque são
mais instrutivos que o resultado final: a auditoria sem autor por causa de
`contextvars` atravessando threadpool, o `rowcount` que devolve -1 no psycopg3, e
duas proteções de banco mutuamente incompatíveis.

---

## 5. O que falta para produção de verdade

Honestidade sobre o alcance. Nada aqui é difícil; está listado porque um projeto
que se apresenta como pronto para empresa precisa dizer onde a linha está.

| Falta | Por que importa | Esforço |
|---|---|---|
| **TLS e proxy reverso** | A API não termina TLS | Baixo — Nginx/Traefik à frente |
| **Migrations como Job separado** | Com várias réplicas, rodar no entrypoint é corrida | Baixo — já anotado no `entrypoint.sh` |
| **Rate limit distribuído** | O limite se multiplica pelo número de réplicas | Baixo — a porta existe |
| **Métricas** | Não há série temporal; só log | Médio — Prometheus + endpoint |
| **Rastreamento distribuído** | Correlação para quando houver mais serviços | Médio — OpenTelemetry |
| **MFA** | Senha comprometida basta para entrar | Médio — TOTP para ADMIN |
| **Retenção de auditoria** | A tabela cresce sem fim | Médio — particionamento + `TRUNCATE` |
| **Backup e restauração testada** | Backup não testado é backup que não existe | Médio — rotina + ensaio |
| **Cifragem de dado em repouso** | Documento legível para quem acessar o disco | Médio — `pgcrypto` ou volume |
| **Alta disponibilidade do banco** | Ponto único de falha | Alto — réplica + failover |

---

## 6. O que este projeto demonstra

Sem inflar, item por item:

- **Python moderno**: tipagem em todas as fronteiras, `Protocol` para inversão
  de dependência, `StrEnum`, `Decimal` onde importa, `contextvars` usado com
  consciência de como ele se comporta no threadpool.
- **FastAPI além do tutorial**: injeção de dependência para transação e
  autorização, middlewares em ordem pensada, ciclo de vida com container,
  tratamento global de exceção em formato padronizado, OpenAPI que descreve os
  erros e não só o caminho feliz.
- **PostgreSQL além do `CREATE TABLE`**: índices parciais e funcionais,
  `ON CONFLICT`, trava consultiva, gatilho em PL/pgSQL, `CHECK` de coerência,
  `JSONB`, tipos de coluna personalizados.
- **Arquitetura**: camadas com dependências invertidas de verdade — verificável
  pelo fato de a suíte unitária rodar sem banco.
- **Segurança**: 13 seções em [SEGURANCA.md](SEGURANCA.md), com modelo de
  ameaças e uma lista explícita do que **não** está coberto.
- **Automação**: idempotente, com exclusão mútua entre processos, tolerante a
  falha individual e com registro de execução consultável.
- **Integração com terceiro**: retry com jitter, disjuntor de três estados,
  distinção entre falha temporária e permanente, chave de idempotência, fila
  morta com ação manual.
- **Testes**: divididos por custo, com dublês que provam que a abstração é real,
  e propriedades no lugar de listas onde isso protege melhor.
- **Documentação**: 12 ADRs com alternativas descartadas e reversibilidade, mais
  a explicação do porquê dentro do próprio código.

O fio condutor: **preferir a garantia à intenção.** Sempre que havia escolha
entre "o código cuida disso" e "o banco impede isso", a segunda foi escolhida —
porque o código muda de mãos e o `psql` não passa por code review.
