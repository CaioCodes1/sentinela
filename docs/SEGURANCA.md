# Segurança

Este documento descreve o que está implementado, **por que** cada controle
existe, e — igualmente importante — **o que não está coberto**. Um documento de
segurança que só lista acertos é propaganda; o que serve para trabalhar é o que
delimita o alcance.

---

## 1. Modelo de ameaças

Quem se defende de tudo não se defende de nada. Os adversários considerados:

| Adversário | Capacidade | Principais controles |
|---|---|---|
| **Atacante externo sem credencial** | Fala com a API pela internet | Rate limit, bloqueio de conta, mensagens que não enumeram, validação estrita, cabeçalhos |
| **Usuário legítimo mal-intencionado** | Tem credencial de um perfil | RBAC por permissão, auditoria de toda ação e de toda negativa, sem escalada por `PATCH` |
| **Atacante com token roubado** | Interceptou refresh token | Rotação com detecção de reúso, hash no banco, TTL curto do access |
| **Quem tem acesso de leitura ao log** | Vê o agregador, não o banco | Redação de CPF, CNPJ, e-mail, JWT e hash |
| **Quem tem acesso direto ao banco** | Abre `psql` | Gatilho de imutabilidade, `CHECK`, senha só como hash, refresh token só como hash |
| **Operador interno tentando apagar o rastro** | Pode ser promovido a ADMIN | A permissão não existe; a proteção é do banco, não do RBAC |

**Fora de escopo, explicitamente:** atacante com acesso ao sistema de arquivos
do servidor (leria o `.env`), comprometimento da cadeia de suprimentos de
dependências, e ataques ao próprio PostgreSQL.

---

## 2. Autenticação

### 2.1 Senhas

- **bcrypt**, custo 12 em produção (o piso recomendado hoje), sal aleatório por
  hash. A aplicação **recusa subir** em produção com custo menor que 12.
- **Limite de 72 bytes respeitado explicitamente.** O bcrypt trunca em 72 bytes
  em silêncio. Aceitar uma senha de 200 caracteres e validar só os 72 primeiros
  dá ao usuário a impressão de uma força que ele não tem. Note que o limite é em
  **bytes**: `ç` e `ã` ocupam dois em UTF-8, então 40 caracteres acentuados já
  podem passar.
- **Hash corrompido não levanta exceção.** Um 500 ali contaria ao atacante que
  aquele usuário existe e está com o registro quebrado — informação que a
  mensagem genérica de login esconde de propósito.
- **Política sem rotação forçada.** Tamanho mínimo, variedade de classes e
  rejeição de sequências óbvias. Rotação a cada 30 dias é contraindicada pelo
  NIST SP 800-63B desde 2017: produz `Senha2026!`, `Senha2027!` e o post-it no
  monitor.

### 2.2 Login que não enumera usuários

Três defesas somadas:

1. **Mensagem única** para e-mail inexistente, senha errada e conta desativada.
2. **Tempo constante.** Quando o e-mail não existe, o sistema executa um
   `bcrypt.checkpw` contra um hash descartável. Sem isso, "e-mail inexistente"
   responde em 1 ms e "senha errada" em 250 ms — diferença medível de fora, que
   transforma o login num verificador de quais e-mails estão cadastrados.
3. **Rate limit específico** de 10/min por IP no `/auth/login`, separado do
   limite geral de 120/min. O limite geral permitiria 120 tentativas de senha
   por minuto, o que não protege nada.

A exceção deliberada: **conta bloqueada** responde 423 com mensagem específica.
A conta é do titular, ele precisa saber por que não entra, e quem disparou o
bloqueio já sabia que ela existe.

### 2.3 Bloqueio de conta

Cinco tentativas erradas bloqueiam por 15 minutos. O detalhe de implementação
que importa: o contador é **confirmado no banco antes** de a exceção ser
levantada. Sem esse commit explícito, o rollback provocado pela exceção zeraria
o contador a cada erro e o bloqueio jamais aconteceria — um defeito que passa
em qualquer teste que use repositório em memória.

### 2.4 JWT

Emissão e validação em `app/core/security.py`. O que é verificado em toda
decodificação:

| Verificação | O que evita |
|---|---|
| Assinatura, com lista fixa de algoritmos | Ataque `alg: none` e confusão de algoritmo |
| `exp`, `nbf`, `iat` obrigatórios | Token sem expiração |
| `iss` | Token emitido por outro sistema |
| `aud` | Token válido de **outra** aplicação que compartilhe o segredo |
| **`typ`** | Refresh token (7 dias) sendo usado como access token — sem esta checagem, o TTL curto do access vira decoração |
| `jti` | Rastreabilidade e revogação por sessão |

Toda falha devolve a **mesma** mensagem. Detalhar ajudaria a calibrar o ataque.

### 2.5 Rotação de refresh token

Descrita em detalhe no [ADR-004](DECISOES.md#adr-004). Em uma frase: um refresh
token vale **uma vez**, tokens do mesmo login formam uma família, e a
reapresentação de um token já revogado derruba a família inteira e vai para a
auditoria como `TOKEN_REUSE_DETECTED`.

O token é persistido como **SHA-256**: um dump do banco não pode virar um
conjunto de sessões válidas.

### 2.6 Troca de senha encerra todas as sessões

Se a troca foi motivada por suspeita de comprometimento, deixar as sessões
antigas vivas esvazia o gesto — o invasor continua dentro com o refresh token
que já tinha.

---

## 3. Autorização

Matriz central em `app/core/permissions.py`, detalhada no
[ADR-010](DECISOES.md#adr-010) e resumida no README.

### O que cada perfil **não** tem, e por quê

**OPERATOR** não tem:

- `audit:read` — quem é auditado não decide o que a auditoria mostra;
- `job:run` — disparar a automação fora de hora envia mensagem real para
  cliente real;
- `user:create` — criar usuário é escalada de privilégio por definição.

**AUDITOR** não tem nenhuma permissão de escrita. Isso é verificado como
propriedade (`permissions_for(AUDITOR) & WRITE_PERMISSIONS == ∅`), não como
lista — permissão nova atribuída por engano reprova sozinha.

**ADMIN** não tem permissão de apagar auditoria, porque ela não existe.

### Negativa é evento auditado

Um 403 sem registro desaparece quando o log de acesso rotaciona. Aqui, toda
negativa grava `ACCESS_DENIED` com o papel do autor e a permissão que faltava —
que é exatamente o que uma investigação procura.

### Defesa contra o esquecimento

`test_toda_rota_exige_autorizacao` varre o aplicativo montado, bate em cada
caminho sem credencial e exige 401. A lista de exceções é explícita. Um
endpoint novo sem proteção reprova o build.

A verificação é sobre **comportamento**, não sobre declaração: conferir a
presença do `Depends` passaria numa rota que declara a dependência e a ignora.

---

## 4. Proteção contra injeção

### SQL

Toda consulta é construída pelo SQLAlchemy com parâmetros ligados. Não existe
concatenação de string com entrada de usuário em nenhum ponto do projeto.

Há um teste que prova isso na prática: buscar clientes pelo termo
`'; DROP TABLE clients; --` devolve zero resultados, e a asserção seguinte
confirma que a tabela continua lá.

**Um degrau além disso:** os curingas do `LIKE` são escapados dentro do termo.
Não é injeção de SQL — os parâmetros continuam corretamente ligados — mas é o
mesmo descuido: tratar entrada do usuário como se fosse sintaxe. Sem escapar,
buscar por `%` devolveria a base inteira, e a paginação só fatiaria o
vazamento.

### Entrada em geral

- **Pydantic v2 com `extra="forbid"`** em todo corpo de requisição. O padrão do
  Pydantic é ignorar campo desconhecido em silêncio, e ignorar em silêncio é
  como um `PATCH` com `{"role": "ADMIN"}` num endpoint que não aceita `role`
  passa despercebido: o campo some, ninguém reclama, e o cliente acha que
  funcionou.
- **Objetos de valor** com validação real: CPF e CNPJ conferidos pelo dígito
  verificador, e-mail normalizado para minúsculas.
- **Caracteres de controle removidos.** Cobre o *bidirectional override*
  (`U+202E`), que faz um nome aparecer invertido na tela sem mudar o byte
  gravado, e o espaço de largura zero, usado para burlar comparação de strings.
- **Regex sem retrocesso catastrófico.** A expressão de e-mail não tem
  quantificador aninhado; há um teste que mede o tempo contra entrada
  patológica. ReDoS é negação de serviço com uma única requisição.
- **`X-Request-Id` saneado.** É texto vindo de fora que vai para o log e para a
  auditoria; sem sanear, uma quebra de linha permitiria forjar linhas de log
  inteiras.

---

## 5. Respostas que não contam demais

| Origem | O que ela traz | O que o cliente recebe |
|---|---|---|
| `IntegrityError` do psycopg | Nome de tabela, de restrição, e o valor que colidiu | `409` com "a operação conflita com o estado atual dos dados" |
| Erro de schema do Pydantic | Inclui `input`, ou seja, **o valor rejeitado** | `422` com campo e motivo, nunca o valor |
| Exceção não tratada | Traceback com caminho de arquivo e variáveis locais | `500` genérico |
| `OperationalError` | String de conexão | `503` com "tente novamente em instantes" |

O `request_id` presente em toda resposta de erro é o que liga a resposta opaca à
entrada de log que tem tudo. O suporte cruza os dois; o atacante, não.

O caso do Pydantic merece destaque: num `POST /auth/login` malformado, devolver
`input` significa devolver **a senha digitada** dentro do corpo da resposta de
erro — que o cliente costuma registrar em log.

---

## 6. Log e dado pessoal

Log é o vazamento de dado pessoal mais comum e o menos notado: ninguém trata
`logger.info(f"cliente {payload}")` como incidente, mas o CPF completo fica no
agregador por meses, visível para muito mais gente do que tem acesso ao banco.

Um filtro roda em **todo** registro — inclusive nos emitidos por biblioteca de
terceiro, que é onde o vazamento aparece sem ninguém ter escrito a linha:

| Padrão | Vira |
|---|---|
| CPF, com ou sem pontuação | `[CPF_REDIGIDO]` |
| CNPJ | `[CNPJ_REDIGIDO]` |
| E-mail | `a***a@empresa.com` (domínio preservado, para investigar sem expor a lista) |
| JWT | `[JWT_REDIGIDO]` |
| `Authorization`, `api_key`, `password`, `senha` | `[REDIGIDO]` |
| Hash bcrypt | `[HASH_REDIGIDO]` |

Além disso:

- Campos proibidos em `extra=` são substituídos antes de serializar, mesmo se
  alguém passar de propósito.
- O **traceback** passa pelo mesmo filtro: ele pode conter valores de variável
  local, inclusive a senha que estava em memória no momento da exceção.
- `sqlalchemy.engine` fica em `WARNING`. Em `INFO`, o SQLAlchemy imprime toda
  consulta com os parâmetros ligados — ou seja, CPF e e-mail em texto puro.

**Na auditoria**, o `details` é podado antes de virar JSONB: chaves proibidas
redigidas, profundidade limitada (JSON aninhado sem fim já derrubou
serializador) e tamanho cortado (um `details` de 2 MB por linha transforma a
auditoria no maior objeto do banco em semanas).

---

## 7. Auditoria

Ver [ADR-003](DECISOES.md#adr-003) e [ADR-011](DECISOES.md#adr-011).

Cada linha responde às cinco perguntas de uma investigação:

| Pergunta | Coluna |
|---|---|
| Quem | `actor_user_id`, `actor_email`, `actor_role` |
| O quê | `action` + `outcome` |
| Quando | `occurred_at` (com fuso) |
| De onde | `ip_address`, `user_agent` |
| Sobre o quê | `resource_type` + `resource_id` |
| Correlação | `request_id` |

`actor_email` e `actor_role` são **cópias do momento do ato**. Se o usuário for
renomeado ou promovido depois, a trilha continua dizendo quem ele era naquele
dia — que é o ponto de auditoria.

**A auditoria é gravada na mesma transação do ato auditado.** Ou os dois
acontecem, ou nenhum. Gravar em transação separada parece mais robusto ("assim
o log nunca se perde") e produz o pior dos mundos: trilha afirmando que um
contrato foi cancelado quando o cancelamento falhou no commit.

A exceção é a falha de autenticação, que precisa de commit próprio — o fluxo
termina em exceção, e o rollback levaria a linha junto. Ficaria sem nenhum
registro de força bruta, que é exatamente o evento que mais importa registrar.

### Eventos auditados

Autenticação (`LOGIN_SUCCESS`, `LOGIN_FAILURE`, `LOGOUT`, `TOKEN_REFRESH`,
`TOKEN_REUSE_DETECTED`, `ACCOUNT_LOCKED`), autorização (`ACCESS_DENIED`),
domínio (criação, alteração **com o diff**, renovação, cancelamento e
vencimento de contrato; criação, alteração e desativação de cliente),
notificações (`NOTIFICATION_SENT`, `NOTIFICATION_FAILED`) e automação
(`JOB_STARTED`, `JOB_FINISHED`, `JOB_FAILED`).

A automação também tem autor identificado: `system:verificacao-diaria-contratos`.
Deixar o autor nulo encheria a trilha de linhas sem responsável, e ninguém
saberia distinguir "o sistema expirou" de "faltou registrar quem expirou".

---

## 8. Cabeçalhos HTTP

| Cabeçalho | Valor | Motivo |
|---|---|---|
| `Content-Security-Policy` | `default-src 'none'` na API | API JSON não carrega nada |
| `Content-Security-Policy` | política própria em `/docs` | Sem a exceção, o Swagger UI abre **em branco** |
| `X-Content-Type-Options` | `nosniff` | Impede o navegador de adivinhar o tipo |
| `X-Frame-Options` | `DENY` | Clickjacking |
| `Referrer-Policy` | `no-referrer` | Não vaza URL com id para terceiros |
| `Permissions-Policy` | recursos desligados | Barato, e evita surpresa |
| `Cache-Control` | `no-store` | A mesma URL devolve conteúdo diferente por usuário |
| `Strict-Transport-Security` | só em produção | Em `localhost` o navegador **memoriza** e quebra o acesso local |
| `Server` | removido | Não anuncia produto e versão |

O caso da CSP é o mais instrutivo: o valor correto para uma API JSON é
`default-src 'none'`, e ele quebra a documentação servida pela mesma
aplicação. O sintoma não aparece em nenhum teste automatizado, porque teste de
API não renderiza página — só abrindo no navegador. A solução é CSP estrita
para tudo com exceção declarada para `/docs`, e não afrouxar a política inteira.

---

## 9. Contêiner

- Imagem multi-estágio: compilador e cabeçalhos ficam no estágio de build.
  Deixá-los na imagem final dá um compilador a quem conseguir execução dentro
  do contêiner.
- **Usuário sem privilégios** (`uid 10001`). Contêiner rodando como root
  significa que uma falha de execução remota começa como root — a correção mais
  barata de segurança de contêiner que existe, e a mais esquecida.
- `read_only: true`, com `tmpfs` só em `/tmp`.
- `cap_drop: ALL` e `no-new-privileges`.
- Limite de memória: sem ele, um vazamento consome a RAM do host em vez de
  derrubar só o contêiner.
- Portas publicadas em `127.0.0.1`. Sem isso, o Docker abre a porta em todas as
  interfaces — inclusive na rede da cafeteria.
- `HEALTHCHECK` aponta para `/health/live`, que não toca no banco.

---

## 10. Segredos

- `.env` fora do controle de versão (`.gitignore`) **e** fora da imagem
  (`.dockerignore`). Segredo embutido em camada continua recuperável mesmo
  depois de a camada ser apagada.
- `SecretStr` no `Settings`: o valor não aparece em `repr` nem em traceback.
- `python -m app.cli gerar-segredo` usa `secrets`, nunca `random`.
- A senha do CLI é lida por `getpass`, nunca por argumento — o que se digita na
  linha de comando fica no histórico do shell e aparece em `ps aux` para
  qualquer usuário da máquina.
- Com `APP_ENV=production`, a aplicação **recusa subir** se: o `JWT_SECRET` for
  o valor de exemplo ou tiver menos de 32 caracteres, a `NOTIFIER_API_KEY` for
  o valor de exemplo, `BCRYPT_ROUNDS` for menor que 12, `DEBUG` estiver ligado,
  ou `CORS_ORIGINS` for `*`.

Falhar no boot, barulhento, é melhor que subir em produção assinando token com
a chave que está publicada no `.env.example`.

---

## 11. Rate limit e limites de recurso

| Limite | Valor | Onde |
|---|---|---|
| Requisições por IP | 120/min | Middleware, antes de tocar no banco |
| Tentativas de login por IP | 10/min | Dependência da rota |
| Tentativas por conta | 5, depois 15 min de bloqueio | Banco (compartilhado entre réplicas) |
| Corpo da requisição | 256 KB | Middleware |
| Página de listagem | 200 itens | Schema **e** repositório |
| Chaves no limitador | 50.000 | Evita que a defesa vire o vetor |
| `statement_timeout` | 15 s | Conexão |
| `idle_in_transaction_session_timeout` | 30 s | Conexão |

Dois detalhes:

O teto de chaves do limitador existe porque, sem ele, um atacante variando o IP
de origem faz o dicionário crescer sem limite — o mecanismo de defesa vira o
vetor de exaustão de memória.

O teto de página vive em dois lugares de propósito: o schema protege a fronteira
HTTP, e o repositório protege as chamadas internas (job, CLI) que não passam
por ali.

---

## 12. Concorrência

- **Trava otimista** nos contratos: o cliente envia a versão que leu, e uma
  alteração concorrente responde 409 em vez de sobrescrever em silêncio. Sem
  ela, o operador que reajustou o valor descobre semanas depois que a alteração
  dele sumiu.
- **Trava consultiva** no PostgreSQL para o job diário.
- **Idempotência no banco**, não na memória do processo — dois processos não
  compartilham memória, mas compartilham o banco.

---

## 13. O que **não** está coberto

Lista honesta do que falta para um ambiente de produção real:

| Ausente | Consequência | Caminho |
|---|---|---|
| TLS terminado pela aplicação | Depende de proxy reverso à frente | Nginx, Traefik ou balanceador gerenciado |
| Autenticação multifator | Senha comprometida basta para entrar | TOTP como segundo fator para ADMIN |
| Rate limit distribuído | O limite se multiplica pelo número de réplicas | `RedisRateLimiter` implementando a porta existente |
| Revogação imediata de access token | Janela de até 15 minutos | Lista de bloqueio por `jti` — ver [ADR-005](DECISOES.md#adr-005) |
| Cifragem de dado em repouso | Documento legível para quem tiver acesso ao disco | `pgcrypto` na coluna, ou cifragem de volume |
| Retenção e expurgo da auditoria | A tabela cresce sem fim | Rotina periódica usando `TRUNCATE` particionado |
| Assinatura da imagem e SBOM | Sem procedência verificável | `cosign` + `syft` no CI |
| Varredura de dependências | CVE nova passa despercebida | `pip-audit` ou Trivy como portão |
| Detecção de anomalia | Auditoria registra, ninguém olha | Exportar para SIEM |

Nenhum desses itens é difícil isoladamente. Estão listados porque um projeto
que se apresenta como seguro precisa dizer onde a segurança termina.
