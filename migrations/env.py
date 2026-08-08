from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# SQLite の ALTER TABLE 制限が厳しいため、batch モードを必須にする
# （docs/phase2_指示書.md §5.1）。カラム変更・制約変更は Phase 2 以降必ず発生する。
from sluicery.db.models import metadata_obj  # noqa: E402

target_metadata = metadata_obj


def _get_db_url() -> str:
    """DB のパスを決定する。

    テスト等から `-x db_path=...` で明示的に上書きできるようにし、それ以外は
    `sluicery.config.Settings`（`.env` / 環境変数）の DB_PATH を使う。
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_path" in x_args:
        return f"sqlite:///{x_args['db_path']}"

    from sluicery.config import Settings

    return f"sqlite:///{Settings().DB_PATH}"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=_get_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _get_db_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
