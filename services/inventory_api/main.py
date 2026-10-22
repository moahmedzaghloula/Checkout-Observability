"""Inventory API application."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shared.config import Settings
from shared.errors import error_response, install_validation_handler
from shared.models import (
    ErrorResponse,
    HealthResponse,
    InventoryAvailability,
    InventoryCheckRequest,
    InventoryCheckResponse,
    Scenario,
)


SERVICE_NAME = "inventory-api"
CATALOG: dict[str, int] = {
    "SKU-001": 10,
    "SKU-002": 0,
    "SKU-003": 25,
}


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    application = FastAPI(title="Inventory API", version="0.1.0")
    install_validation_handler(application, SERVICE_NAME)

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(service=SERVICE_NAME)

    @application.post(
        "/check",
        response_model=InventoryCheckResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def check_inventory(
        request: InventoryCheckRequest,
    ) -> InventoryCheckResponse | JSONResponse:
        stock = CATALOG.get(request.product_id)
        if stock is None:
            return error_response(
                service=SERVICE_NAME,
                status_code=404,
                code="product_not_found",
                message="The requested product does not exist",
            )

        if request.scenario in {Scenario.SLOW, Scenario.ERROR}:
            if not runtime_settings.enable_local_simulations:
                return error_response(
                    service=SERVICE_NAME,
                    status_code=400,
                    code="simulation_disabled",
                    message="Slow and error scenarios are disabled",
                )

        if request.scenario is Scenario.SLOW:
            await asyncio.sleep(runtime_settings.simulated_delay_seconds)
        elif request.scenario is Scenario.ERROR:
            return error_response(
                service=SERVICE_NAME,
                status_code=503,
                code="simulated_inventory_failure",
                message="The inventory failure was simulated locally",
            )

        available = (
            request.scenario is not Scenario.OUT_OF_STOCK
            and stock >= request.quantity
        )
        return InventoryCheckResponse(
            data=InventoryAvailability(
                product_id=request.product_id,
                requested_quantity=request.quantity,
                available=available,
                available_stock=stock,
            )
        )

    return application


app = create_app()
