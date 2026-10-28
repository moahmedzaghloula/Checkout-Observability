import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from shared.logging import JsonFormatter


def _format(formatter: JsonFormatter, message: str) -> dict[str, object]:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    return json.loads(formatter.format(record))


def test_json_logs_correlate_only_with_the_active_span() -> None:
    formatter = JsonFormatter("checkout-api", "test")
    tracer = TracerProvider().get_tracer("logging.tests")

    before = _format(formatter, "before")
    with tracer.start_as_current_span("active") as span:
        active = _format(formatter, "active")
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        expected_span_id = format(span.get_span_context().span_id, "016x")
    after = _format(formatter, "after")

    assert before["trace_id"] is None
    assert before["span_id"] is None
    assert active["trace_id"] == expected_trace_id
    assert active["span_id"] == expected_span_id
    assert after["trace_id"] is None
    assert after["span_id"] is None
    assert active["service.name"] == "checkout-api"
    assert active["service.version"] == "0.1.0"
    assert active["deployment.environment.name"] == "test"
