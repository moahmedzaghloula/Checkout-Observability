"""Demonstrate bounded telemetry outages without failing application traffic."""

from __future__ import annotations

import argparse
from datetime import datetime
import subprocess
import sys
from typing import Any

try:
    from scripts.verification_common import (
        jaeger_trace,
        parse_compose_command,
        poll,
        recent_json_logs,
        request_json,
        request_status,
        require_local_http_url,
        run_compose,
        service_logs,
        utc_now,
    )
except ModuleNotFoundError:
    from verification_common import (  # type: ignore[no-redef]
        jaeger_trace,
        parse_compose_command,
        poll,
        recent_json_logs,
        request_json,
        request_status,
        require_local_http_url,
        run_compose,
        service_logs,
        utc_now,
    )


EXPORT_FAILURE_MARKERS = (
    "failed to export",
    "exception while exporting",
    "connection refused",
    "exporting failed",
    "will retry",
)


def send_normal(checkout_url: str) -> tuple[int, dict[str, Any]]:
    return request_json(
        f"{checkout_url}/checkout",
        method="POST",
        payload={"product_id": "SKU-001", "quantity": 2, "scenario": "normal"},
    )


def latest_accepted_trace_id(
    compose_command: list[str], since: datetime
) -> str | None:
    records = recent_json_logs(compose_command, "checkout-api", since)
    matches = [
        record
        for record in records
        if record.get("event") == "checkout.completed"
        and record.get("outcome") == "accepted"
        and isinstance(record.get("trace_id"), str)
    ]
    return matches[-1]["trace_id"] if matches else None


def contains_export_failure(log_text: str) -> bool:
    normalized = log_text.lower()
    return any(marker in normalized for marker in EXPORT_FAILURE_MARKERS)


def wait_for_collector(
    compose_command: list[str], deadline_seconds: float
) -> str:
    code = (
        "import urllib.request;"
        "print(urllib.request.urlopen("
        "'http://otel-collector:13133/status',timeout=2).status)"
    )

    def collector_status() -> str | None:
        result = run_compose(
            compose_command,
            "exec",
            "-T",
            "checkout-api",
            "python",
            "-c",
            code,
            timeout_seconds=10,
        )
        return result.stdout.strip() if result.stdout.strip() == "200" else None

    return poll(
        "Collector health from checkout network",
        collector_status,
        deadline_seconds=deadline_seconds,
    )


def demonstrate(args: argparse.Namespace) -> None:
    compose_command = parse_compose_command(args.compose_command)
    checkout_url = require_local_http_url(args.checkout_url)
    jaeger_url = require_local_http_url(args.jaeger_url)

    collector_outage_started = utc_now()
    run_compose(
        compose_command,
        "stop",
        "--timeout",
        "10",
        "otel-collector",
        capture_output=False,
    )
    status, body = send_normal(checkout_url)
    if status != 200 or body.get("status") != "ok":
        raise RuntimeError("checkout failed solely because Collector was unavailable")
    print("PASS: checkout returned 200 while Collector was unavailable")

    def application_export_failure() -> str | None:
        combined = service_logs(
            compose_command, "checkout-api", collector_outage_started
        ) + service_logs(compose_command, "inventory-api", collector_outage_started)
        return combined if contains_export_failure(combined) else None

    poll(
        "application exporter failure evidence",
        application_export_failure,
        deadline_seconds=min(args.deadline, 20),
    )
    print("PASS: application logs recorded telemetry export failure evidence")

    run_compose(
        compose_command,
        "start",
        "otel-collector",
        capture_output=False,
    )
    wait_for_collector(compose_command, args.deadline)
    recovery_started = utc_now()
    recovery_status, _ = send_normal(checkout_url)
    if recovery_status != 200:
        raise RuntimeError("checkout did not recover after Collector restart")
    recovery_trace_id = poll(
        "recovery trace ID in structured logs",
        lambda: latest_accepted_trace_id(compose_command, recovery_started),
        deadline_seconds=args.deadline,
    )
    poll(
        "new trace export after Collector recovery",
        lambda: jaeger_trace(jaeger_url, recovery_trace_id),
        deadline_seconds=args.deadline,
    )
    print("PASS: new trace export resumed after Collector recovery")

    run_compose(
        compose_command,
        "stop",
        "--timeout",
        "10",
        "jaeger",
        capture_output=False,
    )
    backend_outage_started = utc_now()
    queued_status, _ = send_normal(checkout_url)
    if queued_status != 200:
        raise RuntimeError("checkout failed while the trace backend was unavailable")
    queued_trace_id = poll(
        "queued trace ID in structured logs",
        lambda: latest_accepted_trace_id(compose_command, backend_outage_started),
        deadline_seconds=args.deadline,
    )

    def collector_failure() -> str | None:
        logs = service_logs(
            compose_command, "otel-collector", backend_outage_started
        )
        return logs if contains_export_failure(logs) else None

    poll(
        "Collector retry evidence while Jaeger is unavailable",
        collector_failure,
        deadline_seconds=min(args.deadline, 20),
    )
    print("PASS: Collector recorded backend export failure and retry evidence")

    run_compose(compose_command, "start", "jaeger", capture_output=False)
    poll(
        "Jaeger health after restart",
        lambda: True if request_status(f"{jaeger_url}/", 3) == 200 else None,
        deadline_seconds=args.deadline,
    )
    poll(
        "queued trace after Jaeger recovery",
        lambda: jaeger_trace(jaeger_url, queued_trace_id),
        deadline_seconds=args.deadline,
    )
    print("PASS: in-memory Collector queue delivered the trace after backend recovery")
    print(
        "LIMIT: this queue is not persistent; a Collector crash can lose buffered telemetry"
    )


def restore_stack(compose_command: list[str], deadline_seconds: float) -> None:
    run_compose(
        compose_command,
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        str(int(deadline_seconds)),
        timeout_seconds=deadline_seconds + 30,
        capture_output=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Demonstrate bounded local telemetry failure behavior."
    )
    parser.add_argument("--compose-command", default="docker compose")
    parser.add_argument("--checkout-url", default="http://127.0.0.1:8000")
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16686")
    parser.add_argument("--deadline", type=float, default=45.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 20 <= args.deadline <= 120:
        print("deadline must be between 20 and 120 seconds", file=sys.stderr)
        return 2
    try:
        compose_command = parse_compose_command(args.compose_command)
        try:
            demonstrate(args)
        finally:
            restore_stack(compose_command, args.deadline)
            print("PASS: project stack restored to a healthy state")
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("Failure demonstrations completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
