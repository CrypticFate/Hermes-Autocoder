import time

from sqlalchemy import select

from autocoder.models import Notification
from autocoder.redaction import redact


def notify(session, message, *, level="info", repo_id=None, task_id=None, key=None):
    if key and session.scalar(select(Notification.id).where(Notification.dedupe_key == key)):
        return
    session.add(Notification(message=redact(message)[:8000], level=level, repository_id=repo_id,
                             task_id=task_id, dedupe_key=key))


def list_notifications(factory, since=0, unacknowledged_only=True):
    with factory() as session:
        query = select(Notification).where(Notification.created_at >= since).order_by(Notification.created_at)
        if unacknowledged_only:
            query = query.where(Notification.acknowledged_at.is_(None))
        return [{"id": n.id, "created_at": n.created_at, "level": n.level, "message": redact(n.message)}
                for n in session.scalars(query.limit(100))]


def acknowledge(factory, ids):
    with factory.begin() as session:
        rows = session.scalars(select(Notification).where(Notification.id.in_(ids))).all()
        for row in rows:
            row.acknowledged_at = time.time()
        return len(rows)
