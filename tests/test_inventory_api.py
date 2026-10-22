import httpx
import pytest

from services.inventory_api.main import create_app
from shared.config import Settings


pytestmark = pytest.mark.asyncio

NORMAL_REQUEST = {
    "product_id": "SKU-001",
    "quantity": 2,
    "scenario": "normal",
}


def inventory_client(settings: Settings | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings or Settings())),
        base_url="http://inventory.test",
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

    async with inventory_client(settings) as client:
        response = await client.post("/check", json=request)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "simulated_inventory_failure"


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
