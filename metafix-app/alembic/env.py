from alembic import context

from app import models  # noqa: F401  (register tables)
from app.db import Base, engine

target_metadata = Base.metadata


def run():
    with engine().connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run()
