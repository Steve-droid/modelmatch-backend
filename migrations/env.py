"""Alembic migration environment.

The database URL comes from app.config (pydantic-settings → DATABASE_URL), never
from alembic.ini — one source of truth across dev / CI / cluster. A migration may
be invoked against a throwaway database by setting DATABASE_URL in the environment
(the round-trip test does exactly this).

S2 hand-writes the schema, so there is no target_metadata / autogenerate yet; the
SQLAlchemy ORM models (and `--autogenerate` support) arrive in S3.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the application's DATABASE_URL so migrations and the app agree on the DB.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to a script without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
