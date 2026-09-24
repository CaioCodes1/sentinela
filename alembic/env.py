"""Ambiente do Alembic.

A URL do banco vem de `Settings`, o mesmo objeto que a aplicação usa. Isso
garante que migration e aplicação nunca apontem para bancos diferentes por
divergência entre o `alembic.ini` e o `.env` — um erro que só se percebe quando
a migration "não fez efeito".
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.types import TypeDecorator

from alembic import context
from app.core.config import get_settings
from app.infrastructure.db.tables import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A URL só é preenchida a partir de `Settings` quando **ninguém já a definiu**.
#
# Quem define antes é o chamador programático — a fixture de teste faz
# `config.set_main_option("sqlalchemy.url", <banco de teste>)` e então chama
# `command.upgrade`. Sobrescrever aqui incondicionalmente faria as migrations
# rodarem no banco de desenvolvimento enquanto os testes olhassem para o banco
# de teste vazio, e o sintoma seria `relação "users" não existe` — com o
# schema criado, só que no lugar errado.
if not config.get_main_option("sqlalchemy.url", None):
    settings = get_settings()
    config.set_main_option("sqlalchemy.url", settings.database_url.get_secret_value())

target_metadata = metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Escreve os tipos personalizados como o tipo SQL que eles de fato são.

    Sem isto, o autogenerate emite
    `app.infrastructure.db.types.StrEnumType(length=40)` dentro da migration —
    ou seja, **a migration passa a importar código da aplicação**. Três
    problemas com isso:

    1. renomear ou apagar `StrEnumType` no futuro quebra uma migration antiga,
       que deveria ser um registro imutável do que já foi aplicado;
    2. o comprimento renderizado vem do `impl` declarado na classe, não do
       tamanho passado na coluna — `role` sairia como `VARCHAR(40)` em vez de
       `VARCHAR(20)`;
    3. rodar migration deixa de ser possível sem o pacote da aplicação
       instalado, o que atrapalha em contêiner de migração enxuto.

    A migration só precisa do tipo físico: `VARCHAR(n)` e `NUMERIC(14,2)`.
    """
    if type_ == "type" and isinstance(obj, TypeDecorator):
        impl = obj.impl if not isinstance(obj.impl, type) else obj.impl()
        name = impl.__class__.__name__
        if name == "String":
            return f"sa.String(length={impl.length})"
        if name == "Numeric":
            return f"sa.Numeric(precision={impl.precision}, scale={impl.scale})"
    return False  # False = use a renderização padrão do Alembic


def run_migrations_offline() -> None:
    """Gera SQL sem conectar — é o modo usado para revisão em mudança controlada."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # `compare_type` e `compare_server_default` ligados: sem eles, o
            # autogenerate ignora mudança de tipo de coluna e de default, e a
            # migration sai incompleta sem avisar.
            compare_type=True,
            compare_server_default=True,
            render_item=render_item,
            # Migration roda dentro de uma transação. No PostgreSQL o DDL é
            # transacional, então uma migration que falha no meio é desfeita
            # inteira — em vez de deixar o schema pela metade.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
