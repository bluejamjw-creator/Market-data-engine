"""Logging with secret redaction.

Every log record passes through `RedactingFilter`, which masks API keys,
the database URL/password and any `apikey=`-style query parameters.
"""
import logging
import re
import sys

from app.config import get_settings

_PARAM_RE = re.compile(
    r"(?i)\b(apikey|api_key|token|password|X-CMC_PRO_API_KEY|authorization)(\s*[=:]\s*)([^\s&'\"]+)"
)
_URL_CRED_RE = re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://)([^:/@\s]+):([^@\s]+)@")


def redact(text: str) -> str:
    """Remove secrets from a string. Safe to call on any text."""
    if not text:
        return text
    for secret in get_settings().secrets():
        if secret in text:
            text = text.replace(secret, "***")
    text = _URL_CRED_RE.sub(r"\1***:***@", text)
    text = _PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class RedactingFormatter(logging.Formatter):
    def formatException(self, ei) -> str:  # noqa: N802
        return redact(super().formatException(ei))


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_mde_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # httpx logs full request URLs at INFO, which can include query-string keys.
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._mde_configured = True  # type: ignore[attr-defined]
