"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_KV_SECRET = re.compile(
    r"(access_token|api_key|api_secret|request_token|authorization|password)(\s*[=:]\s*)([^\s,&\"']+)", re.I
)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***REDACTED***")
        text = _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***REDACTED***", text)
        text = _BEARER.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        if getattr(h, "_atd", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler._atd = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter(secrets))
    root.addHandler(handler)
