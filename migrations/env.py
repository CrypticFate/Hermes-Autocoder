from alembic import context
from sqlalchemy import create_engine

from autocoder.models import Base

config = context.config
engine = create_engine(config.attributes["database_url"])
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
