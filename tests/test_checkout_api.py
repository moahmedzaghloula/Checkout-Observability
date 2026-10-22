from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from services.checkout_api.main import create_app as create_checkout_app
from services.inventory_api.main import create_app as create_inventory_app
from shared.config import Settings


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
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_checkout_app(
        settings or Settings(),
        http_client_factory=factory or inventory_client_factory(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://checkout.test",
        ) as client:
            yield client


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

    async with checkout_client(factory=factory) as client:
        response = await client.post("/checkout", json=CHECKOUT_REQUEST)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "inventory_timeout"


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
