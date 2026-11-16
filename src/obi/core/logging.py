"""Structured (JSON) diagnostic logging (Constitution Principle V).

Kept deliberately separate from the audit trail: logs are diagnostics, the audit trail is
the durable record of actions. Secrets are never logged — credentials are wrapped in
:class:`obi.core.config.Secret`, which masks itself in any string interpolation.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Surface any structured extras passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure and return the ``obi`` logger with a JSON formatter on stderr."""

    logger = logging.getLogger("obi")
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
