"""Shared bounded helpers for local verification scripts."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import subprocess
from time import monotonic, sleep
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


T = TypeVar("T")


def parse_compose_command(value: str) -> list[str]:
    command = shlex.split(value)
    if command not in (["docker", "compose"], ["podman", "compose"]):
        raise ValueError("compose command must be 'docker compose' or 'podman compose'")
    return command


def run_compose(
    compose_command: list[str],
    *arguments: str,
    timeout_seconds: float = 120,
    capture_output: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*compose_command, *arguments],
        check=True,
        capture_output=capture_output,
        text=True,
        timeout=timeout_seconds,
        env=environment,
    )


def require_local_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("verification endpoints must use HTTP on localhost")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("verification endpoints must not contain credentials or queries")
    return value.rstrip("/")


def poll(
    description: str,
    action: Callable[[], T | None],
    *,
    deadline_seconds: float,
    interval_seconds: float = 1.0,
) -> T:
    deadline = monotonic() + deadline_seconds
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            result = action()
            if result is not None:
                return result
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            last_error = exc
        sleep(interval_seconds)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 5,
    basic_auth: tuple[str, str] | None = None,
) -> tuple[int, Any]:
    headers = {"accept": "application/json"}
    data = None
    if payload is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(payload).encode()
    if basic_auth is not None:
        credential = base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode()
        ).decode()
        headers["authorization"] = f"Basic {credential}"
    request = Request(url, method=method, headers=headers, data=data)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def request_status(url: str, timeout_seconds: float = 5) -> int:
    request = Request(url, method="GET", headers={"accept": "*/*"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def prometheus_value(prometheus_url: str, expression: str) -> float | None:
    _, payload = request_json(
        f"{prometheus_url}/api/v1/query?{urlencode({'query': expression})}"
    )
    results = payload["data"]["result"]
    if not results:
        return None
    return float(results[0]["value"][1])


def jaeger_trace(jaeger_url: str, trace_id: str) -> dict[str, Any] | None:
    status, payload = request_json(f"{jaeger_url}/api/traces/{trace_id}")
    if status == 404 or not payload.get("data"):
        return None
    return payload["data"][0]


def jaeger_search(
    jaeger_url: str,
    *,
    service: str,
    operation: str,
    start_microseconds: int,
    end_microseconds: int,
    limit: int,
) -> list[dict[str, Any]]:
    start_time = datetime.fromtimestamp(start_microseconds / 1_000_000, UTC)
    end_time = datetime.fromtimestamp(end_microseconds / 1_000_000, UTC)
    query = urlencode(
        {
            "query.service_name": service,
            "query.operation_name": operation,
            "query.start_time_min": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.start_time_max": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query.search_depth": str(limit),
        }
    )
    status, payload = request_json(f"{jaeger_url}/api/v3/traces?{query}")
    if status != 200:
        return []
    traces: dict[str, dict[str, Any]] = {}
    minimum_start_nanoseconds = start_microseconds * 1_000
    for resource_spans in payload.get("result", {}).get("resourceSpans", []):
        service_name = next(
            (
                attribute["value"].get("stringValue")
                for attribute in resource_spans.get("resource", {}).get(
                    "attributes", []
                )
                if attribute.get("key") == "service.name"
            ),
            None,
        )
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                if int(span.get("startTimeUnixNano", 0)) < minimum_start_nanoseconds:
                    continue
                trace_id = span["traceId"]
                trace_data = traces.setdefault(
                    trace_id,
                    {"traceID": trace_id, "services": set(), "spans": []},
                )
                if service_name:
                    trace_data["services"].add(service_name)
                trace_data["spans"].append(span)
    return list(traces.values())


def recent_json_logs(
    compose_command: list[str], service: str, since: datetime
) -> list[dict[str, Any]]:
    result = run_compose(
        compose_command,
        "logs",
        "--no-color",
        "--since",
        since.astimezone(UTC).isoformat(),
        service,
        timeout_seconds=15,
    )
    records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        opening_brace = line.find("{")
        if opening_brace < 0:
            continue
        try:
            record = json.loads(line[opening_brace:])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def service_logs(
    compose_command: list[str], service: str, since: datetime
) -> str:
    return run_compose(
        compose_command,
        "logs",
        "--no-color",
        "--since",
        since.astimezone(UTC).isoformat(),
        service,
        timeout_seconds=15,
    ).stdout


def read_local_password(path: Path) -> str:
    password = path.read_text().strip()
    if not password:
        raise ValueError(f"password file is empty: {path}")
    return password


def utc_now() -> datetime:
    return datetime.now(UTC)


def span_by_name(trace_data: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [span for span in trace_data["spans"] if span["operationName"] == name]
    if len(matches) != 1:
        raise ValueError(f"expected one span named {name!r}, found {len(matches)}")
    return matches[0]


def span_tag(span: dict[str, Any], key: str) -> Any | None:
    return next(
        (tag.get("value") for tag in span.get("tags", []) if tag.get("key") == key),
        None,
    )


def trace_services(trace_data: dict[str, Any]) -> set[str]:
    return {
        process["serviceName"] for process in trace_data.get("processes", {}).values()
    }
