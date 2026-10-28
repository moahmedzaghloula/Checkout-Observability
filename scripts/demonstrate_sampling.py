"""Demonstrate parent-aware head sampling with real local requests."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import math
import os
import subprocess
import sys
from typing import Any

try:
    from scripts.traffic import generate_traffic, local_checkout_url
    from scripts.verification_common import (
        jaeger_search,
        jaeger_trace,
        parse_compose_command,
        poll,
        prometheus_value,
        recent_json_logs,
        require_local_http_url,
        run_compose,
        utc_now,
    )
except ModuleNotFoundError:
    from traffic import generate_traffic, local_checkout_url  # type: ignore[no-redef]
    from verification_common import (  # type: ignore[no-redef]
        jaeger_search,
        jaeger_trace,
        parse_compose_command,
        poll,
        prometheus_value,
        recent_json_logs,
        require_local_http_url,
        run_compose,
        utc_now,
    )


def recreate_services(
    compose_command: list[str],
    sampler: str,
    sampler_argument: str,
    deadline_seconds: float,
) -> None:
    environment = os.environ.copy()
    environment["OTEL_TRACES_SAMPLER"] = sampler
    environment["OTEL_TRACES_SAMPLER_ARG"] = sampler_argument
    run_compose(
        compose_command,
        "up",
        "--detach",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        str(int(deadline_seconds)),
        "inventory-api",
        "checkout-api",
        timeout_seconds=deadline_seconds + 30,
        capture_output=False,
        environment=environment,
    )


def accepted_metric(prometheus_url: str) -> float | None:
    return prometheus_value(
        prometheus_url,
        'sum(checkout_requests_total{outcome="accepted"})',
    )


def sampled_traces(
    jaeger_url: str,
    started_at: datetime,
    count: int,
) -> list[dict[str, Any]]:
    return jaeger_search(
        jaeger_url,
        service="checkout-api",
        operation="checkout.process",
        start_microseconds=int(started_at.timestamp() * 1_000_000),
        end_microseconds=int((datetime.now(UTC) + timedelta(seconds=5)).timestamp() * 1_000_000),
        limit=count,
    )


def demonstrate(args: argparse.Namespace) -> None:
    compose_command = parse_compose_command(args.compose_command)
    checkout_url = local_checkout_url(args.checkout_url)
    prometheus_url = require_local_http_url(args.prometheus_url)
    jaeger_url = require_local_http_url(args.jaeger_url)

    recreate_services(
        compose_command,
        "parentbased_traceidratio",
        str(args.ratio),
        args.deadline,
    )
    print(
        "PASS: services recreated with parentbased_traceidratio "
        f"and ratio {args.ratio}"
    )

    started_at = utc_now()
    results = asyncio.run(
        generate_traffic(
            checkout_url,
            ["normal"] * args.count,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
        )
    )
    if len(results) != args.count or any(
        result.status_code != 200 for result in results
    ):
        raise RuntimeError("sampling traffic did not complete with all 200 responses")
    print(f"PASS: {args.count} sampled-mode checkout requests returned 200")

    metric_value = poll(
        "sampling-independent custom metric",
        lambda: (
            value
            if (value := accepted_metric(prometheus_url)) is not None
            and abs(value - args.count) < 0.5
            else None
        ),
        deadline_seconds=args.deadline,
    )

    previous_count = -1
    stable_observations = 0

    def stable_traces() -> list[dict[str, Any]] | None:
        nonlocal previous_count, stable_observations
        traces = sampled_traces(jaeger_url, started_at, args.count)
        current_count = len(traces)
        if current_count > 0 and current_count == previous_count:
            stable_observations += 1
        else:
            stable_observations = 0
        previous_count = current_count
        return traces if stable_observations >= 2 else None

    traces = poll(
        "sampled traces to stabilize in Jaeger",
        stable_traces,
        deadline_seconds=args.deadline,
    )
    observed_count = len(traces)
    observed_ratio = observed_count / args.count
    tolerance = max(
        0.10,
        5 * math.sqrt(args.ratio * (1 - args.ratio) / args.count),
    )
    lower_bound = max(0.0, args.ratio - tolerance)
    upper_bound = min(1.0, args.ratio + tolerance)
    if not lower_bound <= observed_ratio <= upper_bound:
        raise RuntimeError(
            "observed sampling ratio is outside the broad statistical sanity bound"
        )
    print(
        f"PASS: observed {observed_count}/{args.count} traces "
        f"({observed_ratio:.3f}) for configured ratio {args.ratio:.3f}"
    )

    for trace_data in traces:
        if trace_data["services"] != {"checkout-api", "inventory-api"}:
            raise RuntimeError("a sampled trace did not contain both services")
        if len(trace_data.get("spans", [])) != 5:
            raise RuntimeError("a sampled trace did not contain all five spans")
    print("PASS: every sampled trace preserved both services and all five spans")
    if int(metric_value) != args.count:
        raise RuntimeError("custom request metric changed with trace sampling")
    print(
        f"PASS: custom metric counted all {int(metric_value)} requests independently"
    )


def restore_full_capture(args: argparse.Namespace) -> None:
    compose_command = parse_compose_command(args.compose_command)
    checkout_url = local_checkout_url(args.checkout_url)
    jaeger_url = require_local_http_url(args.jaeger_url)
    recreate_services(compose_command, "always_on", "", args.deadline)
    restored_at = utc_now()
    results = asyncio.run(
        generate_traffic(
            checkout_url,
            ["normal"],
            timeout_seconds=args.timeout,
            concurrency=1,
        )
    )
    if results[0].status_code != 200:
        raise RuntimeError("full-capture recovery request failed")

    def restored_trace_id() -> str | None:
        records = recent_json_logs(compose_command, "checkout-api", restored_at)
        matches = [
            record
            for record in records
            if record.get("event") == "checkout.completed"
            and record.get("outcome") == "accepted"
        ]
        return matches[-1].get("trace_id") if matches else None

    trace_id = poll(
        "full-capture recovery trace ID",
        restored_trace_id,
        deadline_seconds=args.deadline,
    )
    poll(
        "full-capture recovery trace in Jaeger",
        lambda: jaeger_trace(jaeger_url, trace_id),
        deadline_seconds=args.deadline,
    )
    print("PASS: services restored to always_on and a new trace reached Jaeger")


def count_value(value: str) -> int:
    parsed = int(value)
    if not 100 <= parsed <= 500:
        raise argparse.ArgumentTypeError("count must be between 100 and 500")
    return parsed


def concurrency_value(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 16:
        raise argparse.ArgumentTypeError("concurrency must be between 1 and 16")
    return parsed


def ratio_value(value: str) -> float:
    parsed = float(value)
    if not 0.05 <= parsed <= 0.95:
        raise argparse.ArgumentTypeError("ratio must be between 0.05 and 0.95")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Demonstrate approximate parent-aware head sampling."
    )
    parser.add_argument("--compose-command", default="docker compose")
    parser.add_argument("--checkout-url", default="http://127.0.0.1:8000")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16686")
    parser.add_argument("--count", type=count_value, default=200)
    parser.add_argument("--concurrency", type=concurrency_value, default=8)
    parser.add_argument("--ratio", type=ratio_value, default=0.25)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--deadline", type=float, default=60.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 20 <= args.deadline <= 120:
        print("deadline must be between 20 and 120 seconds", file=sys.stderr)
        return 2
    if not 0 < args.timeout <= 10:
        print("timeout must be greater than 0 and at most 10", file=sys.stderr)
        return 2
    try:
        try:
            demonstrate(args)
        finally:
            restore_full_capture(args)
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("Sampling demonstration completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
