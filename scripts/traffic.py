"""Generate finite, deterministic local checkout traffic."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import math
import random
import sys
from time import perf_counter
from urllib.parse import urlsplit

import httpx


SCENARIOS = ("normal", "slow", "error", "out-of-stock")
DEFAULT_MIX = "normal=50,slow=20,error=15,out-of-stock=15"
EXPECTED_STATUS = {
    "normal": 200,
    "slow": 200,
    "error": 503,
    "out-of-stock": 409,
}
MAX_REQUESTS = 10_000
MAX_CONCURRENCY = 32


@dataclass(frozen=True, slots=True)
class RequestResult:
    scenario: str
    status_code: int | None
    duration_seconds: float
    error_type: str | None = None


def local_checkout_url(target_url: str) -> str:
    """Return the checkout endpoint after enforcing a local-only target."""

    parsed = urlsplit(target_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("target URL must use HTTP on localhost or a loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("target URL must not contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/", "/checkout"}:
        raise ValueError("target URL path must be empty, /, or /checkout")
    base_url = target_url.rstrip("/")
    return base_url if parsed.path == "/checkout" else f"{base_url}/checkout"


def parse_mix(raw_mix: str) -> dict[str, int]:
    """Parse bounded scenario weights from a comma-separated value."""

    weights = {scenario: 0 for scenario in SCENARIOS}
    for item in raw_mix.split(","):
        name, separator, raw_weight = item.strip().partition("=")
        if not separator or name not in weights:
            raise ValueError(
                "mix must contain comma-separated scenario=weight entries"
            )
        if weights[name] != 0:
            raise ValueError(f"scenario {name!r} appears more than once")
        try:
            weight = int(raw_weight)
        except ValueError as exc:
            raise ValueError("scenario weights must be whole numbers") from exc
        if weight < 0:
            raise ValueError("scenario weights must not be negative")
        weights[name] = weight
    if sum(weights.values()) <= 0:
        raise ValueError("at least one scenario weight must be positive")
    return weights


def scenario_plan(count: int, weights: dict[str, int], seed: int) -> list[str]:
    """Allocate weighted scenarios exactly, then shuffle deterministically."""

    total_weight = sum(weights.values())
    exact = {
        scenario: count * weights[scenario] / total_weight for scenario in SCENARIOS
    }
    allocations = {scenario: math.floor(exact[scenario]) for scenario in SCENARIOS}
    remaining = count - sum(allocations.values())
    ranked = sorted(
        SCENARIOS,
        key=lambda scenario: (-(exact[scenario] - allocations[scenario]), scenario),
    )
    for scenario in ranked[:remaining]:
        allocations[scenario] += 1

    plan = [
        scenario
        for scenario in SCENARIOS
        for _ in range(allocations[scenario])
    ]
    random.Random(seed).shuffle(plan)
    return plan


async def generate_traffic(
    checkout_url: str,
    scenarios: list[str],
    *,
    timeout_seconds: float,
    concurrency: int,
) -> list[RequestResult]:
    """Send a finite plan through a bounded worker pool."""

    queue: asyncio.Queue[str] = asyncio.Queue()
    for scenario in scenarios:
        queue.put_nowait(scenario)

    results: list[RequestResult] = []
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds), limits=limits
    ) as client:

        async def worker() -> None:
            while True:
                try:
                    scenario = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started_at = perf_counter()
                try:
                    response = await client.post(
                        checkout_url,
                        json={
                            "product_id": "SKU-001",
                            "quantity": 2,
                            "scenario": scenario,
                        },
                    )
                    results.append(
                        RequestResult(
                            scenario=scenario,
                            status_code=response.status_code,
                            duration_seconds=perf_counter() - started_at,
                        )
                    )
                except httpx.RequestError as exc:
                    results.append(
                        RequestResult(
                            scenario=scenario,
                            status_code=None,
                            duration_seconds=perf_counter() - started_at,
                            error_type=type(exc).__name__,
                        )
                    )
                finally:
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, len(scenarios)))
        ]
        await asyncio.gather(*workers)
    return results


def percentile(values: list[float], percentile_value: float) -> float:
    """Return the nearest-rank percentile for a non-empty sample."""

    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_value * len(ordered)))
    return ordered[rank - 1]


def print_summary(
    checkout_url: str,
    plan: list[str],
    results: list[RequestResult],
) -> int:
    """Print a bounded response and latency summary and return an exit code."""

    statuses = Counter(
        str(result.status_code) if result.status_code is not None else "transport-error"
        for result in results
    )
    errors = Counter(
        result.error_type for result in results if result.error_type is not None
    )
    unexpected = [
        result
        for result in results
        if result.status_code != EXPECTED_STATUS[result.scenario]
    ]
    durations = [result.duration_seconds for result in results]

    print("Traffic summary")
    print(f"  target: {checkout_url}")
    print(f"  planned requests: {len(plan)}")
    print(f"  completed responses: {len(results) - sum(errors.values())}")
    print(f"  transport errors: {sum(errors.values())}")
    print("  scenario counts:")
    for scenario in SCENARIOS:
        print(f"    {scenario}: {plan.count(scenario)}")
    print("  response counts:")
    for status, response_count in sorted(statuses.items()):
        print(f"    {status}: {response_count}")
    if durations:
        print("  latency seconds:")
        print(f"    min: {min(durations):.6f}")
        print(f"    p50: {percentile(durations, 0.50):.6f}")
        print(f"    p95: {percentile(durations, 0.95):.6f}")
        print(f"    max: {max(durations):.6f}")
    if errors:
        print("  transport error types:")
        for error_type, error_count in sorted(errors.items()):
            print(f"    {error_type}: {error_count}")
    if unexpected:
        print(f"  unexpected responses: {len(unexpected)}")
        return 1
    return 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_REQUESTS:
        raise argparse.ArgumentTypeError(
            f"value must be between 1 and {MAX_REQUESTS}"
        )
    return parsed


def bounded_concurrency(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(
            f"concurrency must be between 1 and {MAX_CONCURRENCY}"
        )
    return parsed


def positive_timeout(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 60:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 60")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate finite local checkout traffic; this is not a benchmark."
    )
    parser.add_argument(
        "--target-url",
        required=True,
        help="Local checkout base URL or /checkout endpoint",
    )
    parser.add_argument("--count", type=positive_int, default=20)
    parser.add_argument(
        "--concurrency", type=bounded_concurrency, default=4
    )
    parser.add_argument("--timeout", type=positive_timeout, default=3.0)
    parser.add_argument("--mix", default=DEFAULT_MIX)
    parser.add_argument("--seed", type=int, default=2026)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        checkout_url = local_checkout_url(args.target_url)
        weights = parse_mix(args.mix)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    plan = scenario_plan(args.count, weights, args.seed)
    results = asyncio.run(
        generate_traffic(
            checkout_url,
            plan,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
        )
    )
    return print_summary(checkout_url, plan, results)


if __name__ == "__main__":
    raise SystemExit(main())
