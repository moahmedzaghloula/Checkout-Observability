"""Structured stdout logging with active OpenTelemetry correlation."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import sys
from typing import Any

from opentelemetry import trace

from shared.telemetry import SERVICE_VERSION


_EVENT_FIELDS = (
    "event",
    "outcome",
    "result",
    "status_code",
    "duration_seconds",
)


class JsonFormatter(logging.Formatter):
    """Serialize a bounded application log schema as one JSON object."""

    def __init__(self, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "severity": record.levelname,
            "service.name": self._service_name,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": self._environment,
            "message": record.getMessage(),
            "trace_id": (
                format(span_context.trace_id, "032x") if span_context.is_valid else None
            ),
            "span_id": (
                format(span_context.span_id, "016x") if span_context.is_valid else None
            ),
        }
        for field_name in _EVENT_FIELDS:
            if hasattr(record, field_name):
                payload[field_name] = getattr(record, field_name)
        if record.exc_info:
            payload["exception.type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_application_logging(
    service_name: str, environment: str
) -> logging.Logger:
    """Return one non-propagating application logger with one stdout handler."""

    logger = logging.getLogger(f"checkout_observability.{service_name}")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name, environment))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
