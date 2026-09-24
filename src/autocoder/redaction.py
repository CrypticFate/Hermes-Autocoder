"""Shared redaction for loaded credentials, logs, and outbound text."""
import logging
import re
import threading
import traceback

PATTERNS = [
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_.-]+",
    r"\b(?:sk-or-|sk-|gsk_)[A-Za-z0-9_-]+",
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----[\s\S]*?"
    r"-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    r"(?i)\bBearer\s+[^\s\"'<>]+",
]
_secrets: set[str] = set()
_lock = threading.RLock()


def register_secret(value: str):
    if value:
        with _lock:
            _secrets.add(value)


def redact(text: str, secrets=()) -> str:
    with _lock:
        values = _secrets.union(s for s in secrets if s)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    for pattern in PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text)
    return text


class RedactionFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for name, value in vars(record).items():
            if isinstance(value, str):
                setattr(record, name, redact(value))
        return True


def install_log_redaction():
    loggers = [logging.getLogger(), *(logger for logger in logging.Logger.manager.loggerDict.values()
                                     if isinstance(logger, logging.Logger))]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(f, RedactionFilter) for f in handler.filters):
                handler.addFilter(RedactionFilter())
