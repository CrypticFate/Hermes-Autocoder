import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return uuid.uuid4().hex


def utcnow():
    return datetime.now(timezone.utc)


V2_JSON = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Control(Base):
    __tablename__ = "control"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_micro: Mapped[int | None] = mapped_column(BigInteger)
    monthly_micro: Mapped[int | None] = mapped_column(BigInteger)
    heartbeat: Mapped[float] = mapped_column(Float, default=0)
    discovered_at: Mapped[float] = mapped_column(Float, default=0)
    polled_at: Mapped[float] = mapped_column(Float, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_until: Mapped[float] = mapped_column(Float, default=0)


class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    owner: Mapped[str] = mapped_column(String)
    default_branch: Mapped[str] = mapped_column(String, default="main")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_enabled: Mapped[bool | None] = mapped_column(Boolean)
    accessible: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    scanned_at: Mapped[float] = mapped_column(Float, default=0)
    scanned_sha: Mapped[str | None] = mapped_column(String)
    scanned_context_hash: Mapped[str | None] = mapped_column(String)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_state: Mapped[str] = mapped_column(String, default="active", server_default="active")
    ruleset_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ruleset_report: Mapped[dict | None] = mapped_column(V2_JSON)
    last_plans_scan_sha: Mapped[str | None] = mapped_column(String)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("repo_id", "fingerprint"),
        Index("uq_task_plan", "repo_id", "plan_path", unique=True,
              sqlite_where=text("kind = 'implementation'"),
              postgresql_where=text("kind = 'implementation'")),
        CheckConstraint("state IN ('queued','planning','ready','planned','running','validating','pr_open',"
                        "'awaiting_review','blocked','failed','cancelled','closed_unmerged','merged','pending',"
                        "'preparing','publishing','changes_requested','skipped','invalid')", name="task_state_v2"))
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String, default=uid)
    title: Mapped[str] = mapped_column(String)
    objective: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="User-provided objective")
    acceptance: Mapped[list] = mapped_column(JSON, default=list)
    validation_commands: Mapped[list] = mapped_column(JSON, default=list)
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    fingerprint: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, default="user")
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    branch: Mapped[str | None] = mapped_column(String)
    base_sha: Mapped[str | None] = mapped_column(String)
    tested_sha: Mapped[str | None] = mapped_column(String)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    feedback: Mapped[str] = mapped_column(Text, default="")
    feedback_cursor: Mapped[float] = mapped_column(Float, default=0)
    retry_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    kind: Mapped[str] = mapped_column(String, default="implementation", server_default="implementation")
    plan_path: Mapped[str | None] = mapped_column(String)
    plan_seq: Mapped[int | None] = mapped_column(Integer)
    plan_slug: Mapped[str | None] = mapped_column(String)
    plan_blob_sha: Mapped[str | None] = mapped_column(String)
    plan_title: Mapped[str | None] = mapped_column(String)
    services: Mapped[list] = mapped_column(V2_JSON, default=list, server_default="[]")
    extra_checks: Mapped[list] = mapped_column(V2_JSON, default=list, server_default="[]")
    report_path: Mapped[str | None] = mapped_column(String)
    repair_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    feedback_cursors: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    superseded_pr_number: Mapped[int | None] = mapped_column(Integer)
    rebase_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    mode: Mapped[str] = mapped_column(String, default="implement")
    started_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String, default="running")
    container_id: Mapped[str | None] = mapped_column(String)
    workspace: Mapped[str | None] = mapped_column(String)
    setup: Mapped[list] = mapped_column(JSON, default=list)
    baseline: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    changed_files: Mapped[list] = mapped_column(JSON, default=list)
    report_path: Mapped[str | None] = mapped_column(String)
    publication_sha: Mapped[str | None] = mapped_column(String)
    ready_for_review: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    feedback_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String, index=True)
    at: Mapped[float] = mapped_column(Float, default=time.time)
    kind: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text)


class Capability(Base):
    __tablename__ = "capabilities"
    digest: Mapped[str] = mapped_column(String, primary_key=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String, default="attempt", server_default="attempt")
    pool: Mapped[str] = mapped_column(String, default="builder", server_default="builder")


class Charge(Base):
    __tablename__ = "charges"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    micro_usd: Mapped[int] = mapped_column(BigInteger)
    reserved_micro: Mapped[int] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="reserved")
    pool: Mapped[str] = mapped_column(String, default="builder", server_default="builder")


class Feedback(Base):
    __tablename__ = "pr_feedback"
    __table_args__ = (UniqueConstraint("task_id", "source", "github_id"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    github_id: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    author_login: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    consumed_by_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.id"))
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)


class Sidecar(Base):
    __tablename__ = "sidecars"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.id"))
    service_name: Mapped[str] = mapped_column(String)
    image: Mapped[str] = mapped_column(String)
    container_id: Mapped[str | None] = mapped_column(String)
    network_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="starting")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    removed_at: Mapped[float | None] = mapped_column(Float)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    level: Mapped[str] = mapped_column(String)
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id"))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    message: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[float | None] = mapped_column(Float)
    dedupe_key: Mapped[str | None] = mapped_column(String, unique=True)
