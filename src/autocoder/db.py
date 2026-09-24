from contextlib import contextmanager

from sqlalchemy import create_engine, event, make_url, select
from sqlalchemy.orm import Session, sessionmaker

from autocoder.config import Settings
from autocoder.models import Base, Control


def session_factory(settings: Settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    url = make_url(settings.database_url)
    if settings.database_password_file:
        from autocoder.config import secret
        url = url.set(password=secret(settings.database_password_file))
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
    return sessionmaker(engine, expire_on_commit=False)


def initialize(factory, settings: Settings, *, testing=False):
    if testing:
        Base.metadata.create_all(factory.kw["bind"])
    with factory.begin() as session:
        if not session.get(Control, 1):
            session.add(Control(id=1, daily_micro=micro(settings.budgets.daily_usd),
                                monthly_micro=micro(settings.budgets.monthly_usd)))


def micro(value):
    return None if value is None else int(value * 1_000_000)


@contextmanager
def locked(factory):
    with factory() as session:
        # SQLite is supported for CLI experiments/tests; production uses PostgreSQL.
        if session.bind.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            control = session.scalar(select(Control).where(Control.id == 1).with_for_update())
            if control is None:
                raise RuntimeError("Database not initialized; run autocoder init")
            yield session, control
            session.commit()
        except BaseException:
            session.rollback()
            raise


def require_task(session: Session, task_id: str):
    from autocoder.models import Task
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Unknown task: {task_id}")
    return task
