import io
import logging

import pytest

from autocoder.config import secret
from autocoder.redaction import RedactionFilter, install_log_redaction, redact, register_secret


@pytest.mark.parametrize("value", [
    "ghp_" + "x" * 36, "github_pat_" + "x" * 70, "sk-or-" + "x" * 40,
    "Bearer opaque.example.credential", "bearer opaque-credential",
    "ghs_123_header.payload.signature",
])
def test_i14_common_patterns(value):
    assert value not in redact("Before " + value + " after")
    assert "[REDACTED]" in redact(value)


def test_i14_loaded_secrets_and_explicit_values(tmp_path):
    path = tmp_path / "credential"
    value = "unit-test-opaque-credential"
    path.write_text(value)
    assert secret(path) == value
    assert redact("value=" + value) == "value=[REDACTED]"
    assert redact("ephemeral-value", ["ephemeral-value"]) == "[REDACTED]"


def test_i14_logging_arguments_exception_and_all_handlers():
    streams = [io.StringIO(), io.StringIO()]
    handlers = [logging.StreamHandler(stream) for stream in streams]
    logger = logging.getLogger("autocoder.redaction-test")
    old_handlers, old_level, old_propagate = logger.handlers, logger.level, logger.propagate
    logger.handlers, logger.propagate = handlers, False
    logger.setLevel(logging.INFO)
    value = "unit-test-sensitive-log-value"
    register_secret(value)
    try:
        install_log_redaction()
        install_log_redaction()
        assert all(sum(isinstance(f, RedactionFilter) for f in h.filters) == 1 for h in handlers)
        try:
            raise RuntimeError(value)
        except RuntimeError:
            logger.exception("Credential: %s", value)
        for stream in streams:
            assert value not in stream.getvalue()
            assert "[REDACTED]" in stream.getvalue()
            assert "RuntimeError" in stream.getvalue()
    finally:
        logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate


def test_i14_private_key_body_is_redacted():
    value = "-----BEGIN PRIVATE KEY-----\nexample-body\n-----END PRIVATE KEY-----"
    assert "example-body" not in redact(value)
