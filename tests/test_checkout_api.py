from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode
import pytest

from services.checkout_api.main import create_app as create_checkout_app
from services.inventory_api.main import create_app as create_inventory_app
from shared.config import Settings
from shared.telemetry import TelemetryRuntime, build_resource


pytestmark = pytest.mark.asyncio

CHECKOUT_REQUEST = {
    "product_id": "SKU-001",
    "quantity": 2,
    "scenario": "normal",
}


def inventory_client_factory(
    inventory_settings: Settings | None = None,
) -> Callable[[Settings], httpx.AsyncClient]:
    inventory_app = create_inventory_app(inventory_settings or Settings())

    def factory(_settings: Settings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=inventory_app),
            base_url="http://inventory.test",
        )

    return factory


@asynccontextmanager
async def checkout_client(
    settings: Settings | None = None,
    factory: Callable[[Settings], httpx.AsyncClient] | None = None,
    telemetry: TelemetryRuntime | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    runtime_settings = settings or Settings()
    telemetry_runtime = telemetry or make_test_telemetry(runtime_settings)[0]
    app = create_checkout_app(
        runtime_settings,
        http_client_factory=factory or inventory_client_factory(),
        telemetry_factory=lambda _settings: telemetry_runtime,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://checkout.test",
        ) as client:
            yield client


def make_test_telemetry(
    settings: Settings,
) -> tuple[TelemetryRuntime, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=build_resource(settings, "checkout-api"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        TelemetryRuntime(
            service_name="checkout-api",
            provider=provider,
            tracer=provider.get_tracer("checkout-api.tests", "0.1.0"),
        ),
        exporter,
    )


async def test_health_reports_service_name() -> None:
    async with checkout_client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "checkout-api"}


async def test_checkout_accepts_available_product() -> None:
    async with checkout_client() as client:
        response = await client.post("/checkout", json=CHECKOUT_REQUEST)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "checkout-api",
        "data": {
            "checkout_status": "accepted",
            "product_id": "SKU-001",
            "quantity": 2,
        },
    }


async def test_checkout_creates_bounded_process_span() -> None:
    settings = Settings(app_env="test")
    telemetry, exporter = make_test_telemetry(settings)

    async with checkout_client(settings=settings, telemetry=telemetry) as client:
        response = await client.post("/checkout", json=CHECKOUT_REQUEST)

    spans = exporter.get_finished_spans()
    assert response.status_code == 200
    assert len(spans) == 2
    span = next(span for span in spans if span.name == "checkout.process")
    client_span = next(span for span in spans if span.kind is SpanKind.CLIENT)
    assert span.name == "checkout.process"
    assert client_span.context.trace_id == span.context.trace_id
    assert client_span.parent.span_id == span.context.span_id
    assert span.resource.attributes["service.name"] == "checkout-api"
    assert span.resource.attributes["service.version"] == "0.1.0"
    assert span.resource.attributes["deployment.environment.name"] == "test"
    assert span.attributes == {
        "checkout.quantity": 2,
        "checkout.scenario": "normal",
        "checkout.inventory.status_code": 200,
        "checkout.inventory.available": True,
        "checkout.outcome": "accepted",
        "checkout.response.status_code": 200,
    }


async def test_checkout_marks_dependency_failures_as_span_errors() -> None:
    settings = Settings(app_env="local", enable_local_simulations=True)
    telemetry, exporter = make_test_telemetry(settings)
    request = {**CHECKOUT_REQUEST, "scenario": "error"}

    async with checkout_client(
        settings=settings,
        factory=inventory_client_factory(settings),
        telemetry=telemetry,
    ) as client:
        response = await client.post("/checkout", json=request)

    span = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "checkout.process"
    )
    assert response.status_code == 503
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["checkout.outcome"] == "inventory_unavailable"


async def test_checkout_returns_conflict_when_product_is_unavailable() -> None:
    request = {**CHECKOUT_REQUEST, "scenario": "out-of-stock"}

    async with checkout_client() as client:
        response = await client.post("/checkout", json=request)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "out_of_stock"


async def test_checkout_maps_inventory_failure() -> None:
    local_settings = Settings(app_env="local", enable_local_simulations=True)
    factory = inventory_client_factory(local_settings)
    request = {**CHECKOUT_REQUEST, "scenario": "error"}

    async with checkout_client(local_settings, factory) as client:
        response = await client.post("/checkout", json=request)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "inventory_unavailable"


async def test_checkout_maps_inventory_timeout() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    def factory(_settings: Settings) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))

    telemetry, exporter = make_test_telemetry(Settings())
    async with checkout_client(factory=factory, telemetry=telemetry) as client:
        response = await client.post("/checkout", json=CHECKOUT_REQUEST)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "inventory_timeout"
    span = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "checkout.process"
    )
    assert span.status.status_code is StatusCode.ERROR
    assert [event.name for event in span.events] == ["exception"]


async def test_checkout_closes_inventory_client_on_shutdown() -> None:
    created_clients: list[httpx.AsyncClient] = []

    def factory(_settings: Settings) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_inventory_app(Settings())),
            base_url="http://inventory.test",
        )
        created_clients.append(client)
        return client

    async with checkout_client(factory=factory):
        assert created_clients[0].is_closed is False

    assert created_clients[0].is_closed is True


async def test_checkout_shuts_down_telemetry() -> None:
    telemetry, _exporter = make_test_telemetry(Settings())

    async with checkout_client(telemetry=telemetry):
        assert telemetry._is_shutdown is False

    assert telemetry._is_shutdown is True
