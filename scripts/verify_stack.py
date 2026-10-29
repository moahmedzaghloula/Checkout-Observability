"""Verify the local Checkout Observability stack with real evidence."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Any

try:
    from scripts.verification_common import (
        jaeger_trace,
        parse_compose_command,
        poll,
        prometheus_value,
        read_local_password,
        recent_json_logs,
        request_json,
        require_local_http_url,
        run_compose,
        span_by_name,
        span_tag,
        trace_services,
        utc_now,
    )
except ModuleNotFoundError:
    from verification_common import (  # type: ignore[no-redef]
        jaeger_trace,
        parse_compose_command,
        poll,
        prometheus_value,
        read_local_password,
        recent_json_logs,
        request_json,
        require_local_http_url,
        run_compose,
        span_by_name,
        span_tag,
        trace_services,
        utc_now,
    )


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)
    print(f"PASS: {message}")


def outcome_count(prometheus_url: str, outcome: str) -> float:
    return prometheus_value(
        prometheus_url,
        f'sum(checkout_requests_total{{outcome="{outcome}"}})',
    ) or 0.0


def find_log(
    records: list[dict[str, Any]],
    *,
    outcome: str,
    minimum_duration: float = 0.0,
) -> dict[str, Any] | None:
    matches = [
        record
        for record in records
        if record.get("event") == "checkout.completed"
        and record.get("outcome") == outcome
        and float(record.get("duration_seconds", 0)) >= minimum_duration
    ]
    return matches[-1] if matches else None


def validate_connected_trace(trace_data: dict[str, Any]) -> None:
    expected_names = {
        "POST /checkout",
        "checkout.process",
        "POST",
        "POST /check",
        "inventory.lookup",
    }
    spans = trace_data["spans"]
    check(
        {span["operationName"] for span in spans} == expected_names,
        "trace has five expected spans",
    )
    check(
        trace_services(trace_data) == {"checkout-api", "inventory-api"},
        "trace contains both service identities",
    )
    by_name = {span["operationName"]: span for span in spans}
    expected_parent = {
        "checkout.process": "POST /checkout",
        "POST": "checkout.process",
        "POST /check": "POST",
        "inventory.lookup": "POST /check",
    }
    for child_name, parent_name in expected_parent.items():
        references = by_name[child_name].get("references", [])
        check(
            any(
                reference.get("refType") == "CHILD_OF"
                and reference.get("spanID") == by_name[parent_name]["spanID"]
                for reference in references
            ),
            f"{child_name} is a child of {parent_name}",
        )


def complete_checkout_trace(
    jaeger_url: str, trace_id: str
) -> dict[str, Any] | None:
    trace_data = jaeger_trace(jaeger_url, trace_id)
    if trace_data is None:
        return None
    expected_names = {
        "POST /checkout",
        "checkout.process",
        "POST",
        "POST /check",
        "inventory.lookup",
    }
    if {span["operationName"] for span in trace_data["spans"]} != expected_names:
        return None
    return trace_data


def verify(args: argparse.Namespace) -> None:
    compose_command = parse_compose_command(args.compose_command)
    checkout_url = require_local_http_url(args.checkout_url)
    prometheus_url = require_local_http_url(args.prometheus_url)
    jaeger_url = require_local_http_url(args.jaeger_url)
    grafana_url = require_local_http_url(args.grafana_url)

    run_compose(compose_command, "config", "--quiet")
    print("PASS: Compose configuration is valid")
    run_compose(
        compose_command,
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "/otelcol",
        "otel-collector",
        "validate",
        "--config=/etc/otelcol/config.yaml",
    )
    print("PASS: Collector configuration is valid in the pinned image")

    status, health = request_json(f"{checkout_url}/health")
    check(
        status == 200 and health == {"status": "ok", "service": "checkout-api"},
        "checkout health contract",
    )
    inventory_health = run_compose(
        compose_command,
        "exec",
        "-T",
        "inventory-api",
        "python",
        "-c",
        (
            "import json,urllib.request;"
            "print(json.dumps(json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8001/health',timeout=2))))"
        ),
    )
    check(
        json.loads(inventory_health.stdout) == {
            "status": "ok",
            "service": "inventory-api",
        },
        "inventory health contract",
    )

    previous_total = prometheus_value(prometheus_url, "sum(checkout_requests_total)")
    for scenario, expected_status in (
        ("normal", 200),
        ("error", 503),
        ("out-of-stock", 409),
    ):
        status, _ = request_json(
            f"{checkout_url}/checkout",
            method="POST",
            payload={
                "product_id": "SKU-001",
                "quantity": 2,
                "scenario": scenario,
            },
        )
        check(status == expected_status, f"{scenario} metrics warm-up request")

    def current_process_metrics() -> float | None:
        current_total = prometheus_value(
            prometheus_url, "sum(checkout_requests_total)"
        )
        if current_total is None:
            return None
        if previous_total is not None and current_total == previous_total:
            status, _ = request_json(
                f"{checkout_url}/checkout",
                method="POST",
                payload={
                    "product_id": "SKU-001",
                    "quantity": 2,
                    "scenario": "normal",
                },
            )
            if status != 200:
                raise RuntimeError("metrics warm-up probe did not return 200")
            return None
        return current_total

    baseline_total = poll(
        "current-process metric samples",
        current_process_metrics,
        deadline_seconds=args.deadline,
    )
    baseline_outcomes = {
        outcome: outcome_count(prometheus_url, outcome)
        for outcome in ("accepted", "inventory_unavailable", "out_of_stock")
    }
    baseline_histogram_count = prometheus_value(
        prometheus_url, "sum(checkout_request_duration_seconds_count)"
    ) or 0.0
    baseline_histogram_sum = prometheus_value(
        prometheus_url, "sum(checkout_request_duration_seconds_sum)"
    ) or 0.0

    started_at = utc_now()
    contracts = [
        ("normal", 200, "ok", None),
        ("slow", 200, "ok", None),
        ("error", 503, "error", "inventory_unavailable"),
        ("out-of-stock", 409, "error", "out_of_stock"),
    ]
    observed_latency: dict[str, float] = {}
    for scenario, expected_status, expected_envelope, expected_code in contracts:
        request_started = perf_counter()
        status, body = request_json(
            f"{checkout_url}/checkout",
            method="POST",
            payload={
                "product_id": "SKU-001",
                "quantity": 2,
                "scenario": scenario,
            },
        )
        observed_latency[scenario] = perf_counter() - request_started
        check(status == expected_status, f"{scenario} HTTP status is {expected_status}")
        check(body.get("status") == expected_envelope, f"{scenario} response envelope")
        if expected_code:
            check(body["error"]["code"] == expected_code, f"{scenario} error code")
    check(observed_latency["slow"] >= 0.75, "slow response includes simulated latency")

    def metrics_updated() -> float | None:
        current = prometheus_value(prometheus_url, "sum(checkout_requests_total)")
        if current is not None and current >= baseline_total + 4:
            return current
        return None

    updated_total = poll(
        "checkout metric updates",
        metrics_updated,
        deadline_seconds=args.deadline,
    )
    check(updated_total >= baseline_total + 4, "counter includes all four validated outcomes")
    check(
        outcome_count(prometheus_url, "accepted") >= baseline_outcomes["accepted"] + 2,
        "accepted metric includes normal and slow",
    )
    check(
        outcome_count(prometheus_url, "inventory_unavailable")
        >= baseline_outcomes["inventory_unavailable"] + 1,
        "error metric includes simulated inventory failure",
    )
    check(
        outcome_count(prometheus_url, "out_of_stock")
        >= baseline_outcomes["out_of_stock"] + 1,
        "error metric includes out of stock",
    )
    histogram_count = prometheus_value(
        prometheus_url, "sum(checkout_request_duration_seconds_count)"
    ) or 0.0
    histogram_sum = prometheus_value(
        prometheus_url, "sum(checkout_request_duration_seconds_sum)"
    ) or 0.0
    check(
        histogram_count >= baseline_histogram_count + 4,
        "histogram counts the same validated requests",
    )
    check(
        histogram_sum >= baseline_histogram_sum + 0.75,
        "histogram sum reflects slow-request seconds",
    )
    # A newly created Prometheus label series needs a scraped baseline followed by
    # another increment before rate() can observe a non-zero change. The checks
    # above establish that baseline; this bounded mixed probe creates the second
    # sample needed to verify the dashboard's rate-based queries from a fresh
    # Prometheus volume.
    for scenario, expected_status in (("normal", 200), ("error", 503)):
        status, _ = request_json(
            f"{checkout_url}/checkout",
            method="POST",
            payload={
                "product_id": "SKU-001",
                "quantity": 2,
                "scenario": scenario,
            },
        )
        check(status == expected_status, f"{scenario} dashboard-query probe")

    def dashboard_query_values() -> tuple[float, float, float] | None:
        request_rate = prometheus_value(
            prometheus_url, "sum(rate(checkout_requests_total[1m]))"
        )
        error_ratio = prometheus_value(
            prometheus_url,
            'sum(rate(checkout_requests_total{result="error"}[1m])) / '
            "sum(rate(checkout_requests_total[1m]))",
        )
        latency_p95 = prometheus_value(
            prometheus_url,
            "histogram_quantile(0.95, sum by (le) "
            "(rate(checkout_request_duration_seconds_bucket[1m])))",
        )
        if (
            request_rate is not None
            and request_rate > 0
            and error_ratio is not None
            and 0 < error_ratio < 1
            and latency_p95 is not None
            and math.isfinite(latency_p95)
            and latency_p95 > 0
        ):
            return request_rate, error_ratio, latency_p95
        return None

    request_rate, error_ratio, latency_p95 = poll(
        "rate-based Prometheus dashboard queries",
        dashboard_query_values,
        deadline_seconds=args.deadline,
    )
    check(request_rate is not None and request_rate > 0, "request-rate query returns data")
    check(
        error_ratio is not None and 0 < error_ratio < 1,
        "error-ratio query matches mixed outcomes",
    )
    check(
        latency_p95 is not None and math.isfinite(latency_p95) and latency_p95 > 0,
        "histogram P95 query returns seconds",
    )

    def relevant_logs() -> list[dict[str, Any]] | None:
        records = recent_json_logs(compose_command, "checkout-api", started_at)
        if (
            find_log(records, outcome="inventory_unavailable")
            and find_log(records, outcome="accepted", minimum_duration=0.75)
        ):
            return records
        return None

    logs = poll(
        "structured checkout logs",
        relevant_logs,
        deadline_seconds=args.deadline,
    )
    error_log = find_log(logs, outcome="inventory_unavailable")
    slow_log = find_log(logs, outcome="accepted", minimum_duration=0.75)
    assert error_log is not None and slow_log is not None
    for record in (error_log, slow_log):
        check(record.get("severity") == "INFO", "structured log severity")
        check(record.get("service.name") == "checkout-api", "structured log service identity")
        check(len(str(record.get("trace_id"))) == 32, "structured log trace ID")
        check(len(str(record.get("span_id"))) == 16, "structured log span ID")

    error_trace = poll(
        "error trace in Jaeger",
        lambda: complete_checkout_trace(jaeger_url, str(error_log["trace_id"])),
        deadline_seconds=args.deadline,
    )
    validate_connected_trace(error_trace)
    error_process = span_by_name(error_trace, "checkout.process")
    inventory_lookup = span_by_name(error_trace, "inventory.lookup")
    check(error_process["spanID"] == error_log["span_id"], "log span ID identifies checkout.process")
    check(
        span_tag(error_process, "checkout.outcome") == "inventory_unavailable",
        "error trace records checkout outcome",
    )
    check(
        span_tag(error_process, "otel.status_code") == "ERROR",
        "checkout error span has ERROR status",
    )
    check(
        span_tag(inventory_lookup, "inventory.outcome") == "simulated_failure",
        "inventory failure is visible in its lookup span",
    )

    slow_trace = poll(
        "slow trace in Jaeger",
        lambda: complete_checkout_trace(jaeger_url, str(slow_log["trace_id"])),
        deadline_seconds=args.deadline,
    )
    slow_lookup = span_by_name(slow_trace, "inventory.lookup")
    check(
        slow_lookup["duration"] >= 750_000,
        "slow delay is inside inventory.lookup span",
    )

    password = read_local_password(Path(args.grafana_password_file))
    grafana_auth = (args.grafana_user, password)
    dashboard_status, dashboard_payload = request_json(
        f"{grafana_url}/api/dashboards/uid/checkout-observability",
        basic_auth=grafana_auth,
    )
    check(dashboard_status == 200, "Grafana dashboard API is accessible")
    check(dashboard_payload["meta"]["provisioned"] is True, "dashboard is provisioned")
    panels = dashboard_payload["dashboard"]["panels"]
    check(len(panels) == 3, "dashboard has three focused panels")
    expressions = {target["expr"] for panel in panels for target in panel["targets"]}
    check(
        any("checkout_requests_total" in expression for expression in expressions),
        "dashboard uses the real counter name",
    )
    check(
        any("histogram_quantile(0.95" in expression for expression in expressions),
        "dashboard uses the real histogram for P95",
    )
    datasource_status, datasource_payload = request_json(
        f"{grafana_url}/api/datasources", basic_auth=grafana_auth
    )
    check(datasource_status == 200, "Grafana data source API is accessible")
    check(
        {item["name"] for item in datasource_payload} == {"Prometheus", "Jaeger"},
        "Prometheus and Jaeger data sources are provisioned",
    )
    print(
        "Observed query values: "
        f"rate={request_rate:.6f} req/s, "
        f"error_ratio={error_ratio:.6f}, "
        f"p95={latency_p95:.6f} s"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the running local stack.")
    parser.add_argument("--compose-command", default="docker compose")
    parser.add_argument("--checkout-url", default="http://127.0.0.1:8000")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16686")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3000")
    parser.add_argument("--grafana-user", default="admin")
    parser.add_argument(
        "--grafana-password-file",
        default=".local-secrets/grafana_admin_password",
    )
    parser.add_argument("--deadline", type=float, default=45.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 5 <= args.deadline <= 180:
        print("deadline must be between 5 and 180 seconds", file=sys.stderr)
        return 2
    try:
        verify(args)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("Verification completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
