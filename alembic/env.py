from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

import jobpilot.db.models  # noqa: F401  (registers tables on SQLModel.metadata)
from jobpilot.db.session import get_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata


def run_migrations_online() -> None:
    # Same engine factory as the app, so WAL and foreign keys are on here too.
    engine = get_engine(config.get_main_option("sqlalchemy.url") or None)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
