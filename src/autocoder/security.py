from pathlib import Path

from autocoder.redaction import PATTERNS as PATTERNS
from autocoder.redaction import redact as redact


def protected(path: str) -> bool:
    return (path == ".git" or path.startswith(".git/") or "/.git/" in path
            or Path(path).name in {".env", "key.text"}
            or path.endswith((".pem", ".key")))


def safe_write(root: Path, relative: str, content: str):
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes workspace")
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError("Artifact path contains a symlink")
        current = current.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
