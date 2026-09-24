"""Definição das tabelas em estilo imperativo (`Table`), não declarativo.

Cada restrição aqui é uma regra que o banco passa a garantir **para todo mundo**
— inclusive para o script de importação que alguém roda às pressas, para o
`psql` aberto em produção e para a próxima aplicação que apontar para este
banco. Validação em Pydantic protege a API; `CHECK` protege o dado.

As convenções de nome (`naming_convention`) existem por causa do Alembic: sem
elas, o PostgreSQL batiza as restrições sozinho e a migration de *downgrade*
tenta remover um `ck_contracts_1` que em outro ambiente virou `ck_contracts_2`.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.domain.enums import (
    AuditAction,
    AuditOutcome,
    ClientStatus,
    ContractStatus,
    InstallmentStatus,
    JobStatus,
    NotificationStatus,
    NotificationType,
    Role,
)
from app.infrastructure.db.types import EmailType, MoneyType, StrEnumType, TaxIdType

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

# `JSONB` só existe no PostgreSQL; `JSON` genérico é o fallback que mantém o
# schema carregável em outros dialetos (útil para inspeção offline).
JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")

# Timestamps sempre COM fuso. `TIMESTAMP WITHOUT TIME ZONE` é a origem do bug em
# que o contrato vence uma hora mais tarde entre outubro e fevereiro, quando o
# servidor está num fuso com horário de verão. Tudo é gravado em UTC.
TS = DateTime(timezone=True)


users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    # O índice único é sobre `lower(email)`, criado na migration: garantir a
    # unicidade só na coluna deixaria `Ana@x.com` e `ana@x.com` coexistirem.
    Column("email", EmailType, nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("full_name", String(120), nullable=False),
    Column("role", StrEnumType(Role, 20), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("failed_login_attempts", Integer, nullable=False, server_default=text("0")),
    Column("locked_until", TS, nullable=True),
    Column("last_login_at", TS, nullable=True),
    Column("password_changed_at", TS, nullable=False, server_default=text("now()")),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    CheckConstraint("role IN ('ADMIN','OPERATOR','AUDITOR')", name="role_valido"),
    CheckConstraint("failed_login_attempts >= 0", name="tentativas_nao_negativas"),
    CheckConstraint("length(password_hash) >= 20", name="hash_nao_e_texto_puro"),
    # Unicidade que ignora maiúsculas. Declarada aqui, e não só na migration,
    # para que `alembic check` consiga comparar modelo e banco — um índice
    # que existe apenas na migration aparece como "removido" a cada execução
    # e transforma o portão de deriva em ruído que todo mundo ignora.
    Index("uq_users_email_lower", func.lower(Column("email", EmailType)), unique=True),
)

refresh_tokens = Table(
    "refresh_tokens",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "user_id",
        UUID(as_uuid=True),
        # CASCADE: token é dado derivado da conta. Apagada a conta, nenhuma
        # sessão dela pode continuar valendo.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Guardamos o SHA-256, nunca o token. 64 hexadecimais, tamanho fixo.
    Column(
        "token_hash",
        String(64),
        nullable=False,
        unique=True,
        comment="SHA-256 do token. O token em claro nunca é persistido.",
    ),
    # A família liga todos os tokens descendentes de um mesmo login. É o que
    # permite revogar a sessão inteira quando um token antigo reaparece.
    Column("family_id", UUID(as_uuid=True), nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS, nullable=True),
    Column("replaced_by_id", UUID(as_uuid=True), nullable=True),
    Column("created_ip", String(45), nullable=True),  # 45 = IPv6 com mapeamento IPv4
    Column("user_agent", String(300), nullable=True),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Index("ix_refresh_tokens_family_id", "family_id"),
    Index("ix_refresh_tokens_user_id", "user_id"),
    # Faxina de tokens vencidos: o índice parcial só cobre os ainda vivos, que
    # é a fatia consultada no `/refresh`.
    Index(
        "ix_refresh_tokens_ativos",
        "expires_at",
        postgresql_where=text("revoked_at IS NULL"),
    ),
)

clients = Table(
    "clients",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tax_id", TaxIdType, nullable=False, unique=True),
    Column("legal_name", String(180), nullable=False),
    Column("trade_name", String(180), nullable=True),
    Column("email", EmailType, nullable=False),
    Column("phone", String(13), nullable=True),
    Column(
        "status",
        StrEnumType(ClientStatus, 20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    ),
    Column("created_by_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=True),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valido"),
    CheckConstraint("length(tax_id) IN (11, 14)", name="documento_11_ou_14_digitos"),
    CheckConstraint("tax_id ~ '^[0-9]+$'", name="documento_so_digitos"),
    Index("ix_clients_legal_name", "legal_name"),
    Index("ix_clients_status", "status"),
    # Busca por parte do nome (`ILIKE '%termo%'`) não usa índice B-tree: o
    # curinga à esquerda impede. Trigramas com GIN resolvem.
    Index(
        "ix_clients_legal_name_trgm",
        "legal_name",
        postgresql_using="gin",
        postgresql_ops={"legal_name": "gin_trgm_ops"},
    ),
)

contracts = Table(
    "contracts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "client_id",
        UUID(as_uuid=True),
        # RESTRICT, não CASCADE: apagar um cliente não pode levar junto o
        # histórico contratual dele. Cliente sai de cena por desativação.
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("number", String(40), nullable=False, unique=True),
    Column("description", String(500), nullable=True),
    Column("monthly_amount", MoneyType, nullable=False),
    Column("start_date", Date, nullable=False),
    Column("end_date", Date, nullable=False),
    Column("due_day", Integer, nullable=False),
    Column(
        "status",
        StrEnumType(ContractStatus, 20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    ),
    Column("auto_renew", Boolean, nullable=False, server_default=text("false")),
    Column("renewal_term_months", Integer, nullable=False, server_default=text("12")),
    Column("cancelled_at", TS, nullable=True),
    Column("cancellation_reason", String(300), nullable=True),
    Column("renewed_to_id", UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=True),
    Column("previous_contract_id", UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=True),
    Column(
        "version",
        Integer,
        nullable=False,
        server_default=text("1"),
        comment=(
            "Trava otimista: o UPDATE inclui WHERE version = <lida> e falha se "
            "outra transação gravou antes."
        ),
    ),
    Column("created_by_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=True),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    CheckConstraint(
        "status IN ('ACTIVE','EXPIRING','EXPIRED','RENEWED','CANCELLED')",
        name="status_valido",
    ),
    CheckConstraint("end_date > start_date", name="termino_depois_do_inicio"),
    CheckConstraint("monthly_amount > 0", name="mensalidade_positiva"),
    CheckConstraint("due_day BETWEEN 1 AND 31", name="dia_vencimento_valido"),
    CheckConstraint("renewal_term_months BETWEEN 1 AND 120", name="prazo_renovacao_valido"),
    # Coerência entre status e colunas de apoio: sem isto, é possível existir
    # um contrato CANCELLED sem data de cancelamento, e o relatório de
    # cancelamentos do mês silenciosamente não o conta.
    CheckConstraint(
        "(status <> 'CANCELLED') OR (cancelled_at IS NOT NULL)",
        name="cancelado_tem_data",
    ),
    CheckConstraint(
        "(status <> 'RENEWED') OR (renewed_to_id IS NOT NULL)",
        name="renovado_aponta_sucessor",
    ),
    CheckConstraint("renewed_to_id IS NULL OR renewed_to_id <> id", name="sucessor_nao_e_ele"),
    Index("ix_contracts_client_id", "client_id"),
    # O índice que sustenta a automação: a varredura diária filtra por status
    # aberto e ordena por vencimento. Índice parcial porque contrato encerrado
    # nunca entra nessa consulta — e eles são a maioria com o tempo.
    Index(
        "ix_contracts_vencimento_aberto",
        "end_date",
        postgresql_where=text("status IN ('ACTIVE','EXPIRING')"),
    ),
    Index("ix_contracts_status", "status"),
)

installments = Table(
    "installments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "contract_id",
        UUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("competence", String(7), nullable=False),  # AAAA-MM
    Column("due_date", Date, nullable=False),
    Column("amount", MoneyType, nullable=False),
    Column(
        "status",
        StrEnumType(InstallmentStatus, 20),
        nullable=False,
        server_default=text("'PENDING'"),
    ),
    Column("paid_at", TS, nullable=True),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    # A chave natural da mensalidade. É esta restrição — e não a memória do
    # processo — que garante que rodar o job duas vezes não gera cobrança dupla.
    UniqueConstraint("contract_id", "competence", name="uq_installments_contract_id_competence"),
    CheckConstraint("status IN ('PENDING','PAID','OVERDUE','CANCELLED')", name="status_valido"),
    CheckConstraint("amount > 0", name="valor_positivo"),
    CheckConstraint("competence ~ '^[0-9]{4}-[0-9]{2}$'", name="competencia_formato_aaaa_mm"),
    CheckConstraint("(status <> 'PAID') OR (paid_at IS NOT NULL)", name="paga_tem_data"),
    Index(
        "ix_installments_vencidas",
        "due_date",
        postgresql_where=text("status IN ('PENDING','OVERDUE')"),
    ),
    Index("ix_installments_contract_id", "contract_id"),
)

contract_events = Table(
    "contract_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "contract_id",
        UUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("event_type", String(40), nullable=False),
    Column("payload", JSON_TYPE, nullable=False, server_default=text("'{}'::jsonb")),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=True),
    Column("occurred_at", TS, nullable=False, server_default=text("now()")),
    Index("ix_contract_events_contract_id_occurred_at", "contract_id", "occurred_at"),
    comment="Histórico do contrato. Somente inserção, mesma proteção da auditoria.",
)

notifications = Table(
    "notifications",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "contract_id",
        UUID(as_uuid=True),
        ForeignKey("contracts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("notification_type", StrEnumType(NotificationType, 40), nullable=False),
    Column("channel", String(20), nullable=False),
    Column("recipient", String(254), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("body", Text, nullable=False),
    # A chave de idempotência. `UNIQUE` aqui é o que transforma "o job não pode
    # mandar duas vezes" de intenção em garantia.
    Column(
        "dedupe_key",
        String(200),
        nullable=False,
        unique=True,
        comment=(
            "Chave natural do evento. O índice único aqui é o que torna a automação idempotente."
        ),
    ),
    Column(
        "status",
        StrEnumType(NotificationStatus, 20),
        nullable=False,
        server_default=text("'PENDING'"),
    ),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error", String(500), nullable=True),
    Column("provider_message_id", String(120), nullable=True),
    Column("sent_at", TS, nullable=True),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    CheckConstraint("status IN ('PENDING','SENT','FAILED','DEAD_LETTER')", name="status_valido"),
    CheckConstraint("attempts >= 0", name="tentativas_nao_negativas"),
    CheckConstraint("(status <> 'SENT') OR (sent_at IS NOT NULL)", name="enviada_tem_data"),
    # O índice do despacho: só o que ainda pode ser enviado.
    Index(
        "ix_notifications_a_enviar",
        "created_at",
        postgresql_where=text("status IN ('PENDING','FAILED')"),
    ),
    Index("ix_notifications_contract_id", "contract_id"),
    Index("ix_notifications_status", "status"),
)

audit_logs = Table(
    "audit_logs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("occurred_at", TS, nullable=False, server_default=text("now()")),
    Column("action", StrEnumType(AuditAction, 40), nullable=False),
    Column("outcome", StrEnumType(AuditOutcome, 10), nullable=False),
    Column("resource_type", String(40), nullable=False),
    Column("resource_id", String(64), nullable=True),
    # RESTRICT, e a escolha tem história (ver a migration c9d8e7f6a5b4).
    #
    # CASCADE está fora de questão: apagaria a trilha junto com o auditado, o
    # oposto do propósito da tabela. `SET NULL` parecia a resposta certa e
    # **não funciona aqui**: o PostgreSQL implementa `SET NULL` como um UPDATE
    # na tabela referenciada, e o gatilho de imutabilidade recusa UPDATE. A
    # cláusula existiria sem nunca poder ser executada.
    #
    # `RESTRICT` diz a verdade: usuário com trilha não é removido, é desativado
    # — mesma política aplicada a cliente.
    Column(
        "actor_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        comment=(
            "RESTRICT: usuário com trilha de auditoria não pode ser removido, "
            "apenas desativado. SET NULL era inviável — o UPDATE implícito é "
            "recusado pelo gatilho de imutabilidade."
        ),
    ),
    # Por isso o e-mail e o papel também ficam gravados como texto: eles
    # sobrevivem à remoção do usuário e ao dia em que ele mudar de perfil.
    Column("actor_email", String(254), nullable=True),
    Column("actor_role", StrEnumType(Role, 20), nullable=True),
    Column("ip_address", String(45), nullable=True),
    Column("user_agent", String(300), nullable=True),
    Column("request_id", String(64), nullable=True),
    Column("details", JSON_TYPE, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint("outcome IN ('SUCCESS','FAILURE')", name="desfecho_valido"),
    # A consulta padrão do auditor é "últimas ocorrências": DESC no índice
    # evita ordenação em disco quando a tabela cresce.
    Index("ix_audit_logs_occurred_at", text("occurred_at DESC")),
    Index("ix_audit_logs_actor_user_id_occurred_at", "actor_user_id", text("occurred_at DESC")),
    Index("ix_audit_logs_resource_type_resource_id", "resource_type", "resource_id"),
    Index("ix_audit_logs_action", "action"),
    comment="Trilha de auditoria. Somente inserção: gatilho recusa UPDATE e DELETE.",
)

job_runs = Table(
    "job_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("job_name", String(60), nullable=False),
    Column("status", StrEnumType(JobStatus, 20), nullable=False),
    Column("started_at", TS, nullable=False, server_default=text("now()")),
    Column("finished_at", TS, nullable=True),
    Column("stats", JSON_TYPE, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error_message", String(1000), nullable=True),
    CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','SKIPPED')", name="status_valido"),
    CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at", name="fim_depois_do_inicio"
    ),
    Index("ix_job_runs_job_name_started_at", "job_name", text("started_at DESC")),
)
