from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
import pytest

from services.inventory_api.main import create_app
from shared.config import Settings
from shared.telemetry import TelemetryRuntime, build_resource


pytestmark = pytest.mark.asyncio

NORMAL_REQUEST = {
    "product_id": "SKU-001",
    "quantity": 2,
    "scenario": "normal",
}


@asynccontextmanager
async def inventory_client(
    settings: Settings | None = None,
    telemetry: TelemetryRuntime | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    runtime_settings = settings or Settings()
    telemetry_runtime = telemetry or make_test_telemetry(runtime_settings)[0]
    app = create_app(
        runtime_settings,
        telemetry_factory=lambda _settings: telemetry_runtime,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://inventory.test",
        ) as client:
            yield client


def make_test_telemetry(
    settings: Settings,
) -> tuple[TelemetryRuntime, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=build_resource(settings, "inventory-api"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter_provider = MeterProvider(
        resource=build_resource(settings, "inventory-api"),
        metric_readers=[InMemoryMetricReader()],
    )
    return (
        TelemetryRuntime(
            service_name="inventory-api",
            provider=provider,
            tracer=provider.get_tracer("inventory-api.tests", "0.1.0"),
            meter_provider=meter_provider,
            meter=meter_provider.get_meter("inventory-api.tests", "0.1.0"),
        ),
        exporter,
    )


async def test_health_reports_service_name() -> None:
    async with inventory_client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "inventory-api"}


async def test_repeated_checks_do_not_decrement_stock() -> None:
    async with inventory_client() as client:
        first_response = await client.post("/check", json=NORMAL_REQUEST)
        second_response = await client.post("/check", json=NORMAL_REQUEST)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["data"] == second_response.json()["data"]
    assert second_response.json()["data"] == {
        "product_id": "SKU-001",
        "requested_quantity": 2,
        "available": True,
        "available_stock": 10,
    }


async def test_inventory_creates_lookup_span() -> None:
    settings = Settings(app_env="test")
    telemetry, exporter = make_test_telemetry(settings)

    async with inventory_client(settings, telemetry) as client:
        response = await client.post("/check", json=NORMAL_REQUEST)

    spans = exporter.get_finished_spans()
    assert response.status_code == 200
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "inventory.lookup"
    assert span.resource.attributes["service.name"] == "inventory-api"
    assert span.resource.attributes["service.version"] == "0.1.0"
    assert span.resource.attributes["deployment.environment.name"] == "test"
    assert span.attributes == {
        "inventory.quantity": 2,
        "inventory.scenario": "normal",
        "inventory.available": True,
        "inventory.outcome": "available",
        "inventory.response.status_code": 200,
    }


async def test_out_of_stock_scenario_is_deterministic() -> None:
    request = {**NORMAL_REQUEST, "scenario": "out-of-stock"}

    async with inventory_client() as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 200
    assert response.json()["data"]["available"] is False
    assert response.json()["data"]["available_stock"] == 10


async def test_unknown_product_returns_consistent_error() -> None:
    request = {**NORMAL_REQUEST, "product_id": "SKU-404"}

    async with inventory_client() as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "service": "inventory-api",
        "error": {
            "code": "product_not_found",
            "message": "The requested product does not exist",
        },
    }


async def test_non_positive_quantity_is_rejected() -> None:
    request = {**NORMAL_REQUEST, "quantity": 0}

    async with inventory_client() as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_unknown_scenario_is_rejected() -> None:
    request = {**NORMAL_REQUEST, "scenario": "surprise"}

    async with inventory_client() as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_failure_simulation_is_disabled_by_default() -> None:
    request = {**NORMAL_REQUEST, "scenario": "error"}

    async with inventory_client() as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "simulation_disabled"


async def test_failure_simulation_requires_local_environment() -> None:
    with pytest.raises(ValueError, match="APP_ENV=local"):
        Settings(app_env="production", enable_local_simulations=True)


async def test_local_error_scenario_returns_service_unavailable() -> None:
    settings = Settings(app_env="local", enable_local_simulations=True)
    request = {**NORMAL_REQUEST, "scenario": "error"}

    telemetry, exporter = make_test_telemetry(settings)
    async with inventory_client(settings, telemetry) as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "simulated_inventory_failure"
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["inventory.outcome"] == "simulated_failure"
    assert span.events == ()


async def test_local_slow_scenario_returns_normal_availability() -> None:
    settings = Settings(
        app_env="local",
        enable_local_simulations=True,
        simulated_delay_seconds=0.001,
    )
    request = {**NORMAL_REQUEST, "scenario": "slow"}

    async with inventory_client(settings) as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 200
    assert response.json()["data"]["available"] is True
