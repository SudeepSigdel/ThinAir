"""Alembic environment. The engine is passed in via `config.attributes["engine"]`."""

from alembic import context
from sqlmodel import SQLModel

from thinair.db import models  # noqa: F401  (registers tables on SQLModel.metadata)

target_metadata = SQLModel.metadata
engine = context.config.attributes["engine"]

with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite-friendly ALTERs
    )
    with context.begin_transaction():
        context.run_migrations()
