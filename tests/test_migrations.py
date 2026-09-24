from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from autocoder.models import Base


def test_v1_rows_survive_upgrade_downgrade_and_metadata_matches(tmp_path):
    url = f"sqlite:///{tmp_path}/migrate.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO control VALUES (1,1,100,100,0,0,0,NULL,0)"))
        connection.execute(text("INSERT INTO repositories VALUES "
            "(1,'owner/repo','owner','main',0,NULL,1,'legacy',0,NULL,NULL,NULL,0)"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert connection.execute(text("SELECT queue_state FROM repositories")).scalar() == "active"
    command.downgrade(config, "0001")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT reason FROM repositories")).scalar() == "legacy"
        assert connection.execute(text("SELECT daily_micro FROM control")).scalar() == 100
    command.upgrade(config, "head")
