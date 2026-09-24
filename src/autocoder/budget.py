import hashlib
import json
import math
import secrets
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from autocoder.db import locked
from autocoder.models import Attempt, Capability, Charge, Task


class BudgetError(RuntimeError):
    pass


NEVER_EXPIRES = 1e15  # Concierge capabilities are revoked or rotated, never expired.


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_capability(factory, attempt_id, seconds):
    token = secrets.token_urlsafe(32)
    with factory.begin() as session:
        session.add(Capability(digest=digest(token), attempt_id=attempt_id, expires_at=time.time() + seconds,
                               kind="attempt", pool="builder"))
    return token


def register_concierge_token(factory, token):
    """Store only the hash of the concierge model token, revoking any previous one."""
    if len(token) < 32:
        raise ValueError("Concierge model token must be at least 32 characters")
    with factory.begin() as session:
        for cap in session.scalars(select(Capability).where(Capability.kind == "concierge")):
            cap.revoked = True
        existing = session.get(Capability, digest(token))
        if existing:
            existing.revoked, existing.kind, existing.pool = False, "concierge", "concierge"
            existing.expires_at = NEVER_EXPIRES
        else:
            session.add(Capability(digest=digest(token), attempt_id=None, expires_at=NEVER_EXPIRES,
                                   kind="concierge", pool="concierge"))


def revoke(factory, attempt_id):
    with factory.begin() as session:
        for cap in session.scalars(select(Capability).where(Capability.attempt_id == attempt_id)):
            cap.revoked = True


def period_starts():
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def available(session, control, settings):
    if control.daily_micro is None or control.monthly_micro is None or settings.paid_errors():
        return False
    minimum = math.ceil(4096 * settings.model.input_usd_per_mtok
                        + settings.model.max_output_tokens * settings.model.output_usd_per_mtok)
    for start, ceiling in zip(period_starts(), (control.daily_micro, control.monthly_micro)):
        used = session.scalar(select(func.coalesce(func.sum(Charge.micro_usd), 0)).where(Charge.created_at >= start))
        if used + minimum > ceiling:
            return False
    return True


def pool_usage(session, pool, since):
    return session.scalar(select(func.count()).select_from(Charge).where(
        Charge.pool == pool, Charge.created_at >= since))


def capability_active(session, cap, control):
    """Whether a capability may still spend. Pause stops the builder pool only (Phase 11.6)."""
    if not cap or cap.revoked or cap.expires_at < time.time():
        return False
    if cap.kind == "concierge":
        return True
    attempt = session.get(Attempt, cap.attempt_id)
    task = session.get(Task, attempt.task_id) if attempt else None
    return bool(task and not control.paused and task.state in {"planning", "running", "validating"})


def reserve(factory, settings, token, body):
    if settings.paid_errors():
        raise BudgetError("Model pricing/provider configuration incomplete")
    encoded = json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > 1_000_000:
        raise BudgetError("Model request too large")
    if body.get("model") != settings.model.model:
        raise BudgetError("Model is not configured")
    output = body.get("max_tokens", body.get("max_completion_tokens", settings.model.max_output_tokens))
    if not isinstance(output, int) or not 1 <= output <= settings.model.max_output_tokens:
        raise BudgetError("Invalid output token limit")
    # UTF-8 bytes plus framing allowance conservatively bound text tokenization.
    estimate = math.ceil((len(encoded) + 4096) * settings.model.input_usd_per_mtok
                         + output * settings.model.output_usd_per_mtok)
    day, _ = period_starts()
    with locked(factory) as (session, control):
        cap = session.get(Capability, digest(token))
        if not cap or cap.revoked or cap.expires_at < time.time():
            raise BudgetError("Invalid or expired run capability")
        if not capability_active(session, cap, control):
            raise BudgetError("Run paused or inactive")
        if control.daily_micro is None or control.monthly_micro is None:
            raise BudgetError("Explicit daily and monthly budgets are required")
        pools = settings.budgets.pools
        if cap.kind == "concierge":
            if pool_usage(session, "concierge", day) >= pools.concierge.requests_per_day:
                raise BudgetError("Concierge pool daily request limit reached")
        else:
            if pool_usage(session, "builder", day) >= pools.builder.requests_per_day:
                raise BudgetError("Builder pool daily request limit reached")
            count = session.scalar(select(func.count()).select_from(Charge).where(
                Charge.attempt_id == cap.attempt_id))
            if count >= pools.builder.requests_per_attempt:
                raise BudgetError("Run request limit reached")
        for start, limit in zip(period_starts(), (control.daily_micro, control.monthly_micro)):
            spent = session.scalar(select(func.coalesce(func.sum(Charge.micro_usd), 0)).where(
                Charge.created_at >= start))
            if spent + estimate > limit:
                raise BudgetError("Spending ceiling reached")
        charge = Charge(attempt_id=cap.attempt_id, micro_usd=estimate, reserved_micro=estimate, pool=cap.pool)
        session.add(charge)
        session.flush()
        return charge.id


def usage_by_pool(session):
    day, month = period_starts()
    result = {}
    for pool in ("builder", "concierge"):
        requests = pool_usage(session, pool, day)
        spent = session.scalar(select(func.coalesce(func.sum(Charge.micro_usd), 0)).where(
            Charge.pool == pool, Charge.created_at >= month))
        result[pool] = {"requests_today": int(requests), "usd_this_month": int(spent) / 1_000_000}
    return result


def settle(factory, settings, charge_id, usage=None):
    with locked(factory) as (session, control):
        charge = session.get(Charge, charge_id)
        if charge.state != "reserved":
            return
        if usage and all(isinstance(usage.get(k), int) and usage[k] >= 0
                         for k in ("prompt_tokens", "completion_tokens")):
            charge.input_tokens, charge.output_tokens = usage["prompt_tokens"], usage["completion_tokens"]
            actual = math.ceil(charge.input_tokens * settings.model.input_usd_per_mtok
                               + charge.output_tokens * settings.model.output_usd_per_mtok)
            charge.micro_usd = actual
            charge.state = "measured"
            if actual > charge.reserved_micro:
                control.paused = True
        else:
            # Unknown outcomes remain charged in full, including proxy crashes.
            charge.state = "estimated"
