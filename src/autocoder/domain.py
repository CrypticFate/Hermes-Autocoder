import hashlib

from autocoder.contracts import PlanResult as PlanResult
from autocoder.contracts import ReviewResult as ReviewResult
from autocoder.contracts import RunResult as RunResult
from autocoder.scheduler import TRANSITIONS as TRANSITIONS
from autocoder.scheduler import transition as transition

TERMINAL = {"merged", "cancelled", "closed_unmerged", "failed", "planned", "skipped"}


def fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
