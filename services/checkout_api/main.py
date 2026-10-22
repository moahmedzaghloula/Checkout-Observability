"""Checkout API application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import ValidationError

from shared.config import Settings
from shared.errors import error_response, install_validation_handler
from shared.models import (
    CheckoutRequest,
    CheckoutResponse,
    CheckoutResult,
    ErrorResponse,
    HealthResponse,
    InventoryCheckResponse,
)


SERVICE_NAME = "checkout-api"
HttpClientFactory = Callable[[Settings], httpx.AsyncClient]


def _default_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.inventory_api_url,
        timeout=httpx.Timeout(settings.inventory_http_timeout_seconds),
    )


def create_app(
    settings: Settings | None = None,
    http_client_factory: HttpClientFactory = _default_http_client,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.inventory_client = http_client_factory(runtime_settings)
        try:
            yield
        finally:
            await application.state.inventory_client.aclose()

    application = FastAPI(
        title="Checkout API",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_validation_handler(application, SERVICE_NAME)

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(service=SERVICE_NAME)

    @application.post(
        "/checkout",
        response_model=CheckoutResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def checkout(
        request: CheckoutRequest, http_request: Request
    ) -> CheckoutResponse | JSONResponse:
        client: httpx.AsyncClient = http_request.app.state.inventory_client
        try:
            inventory_http_response = await client.post(
                "/check", json=request.model_dump(mode="json")
            )
        except httpx.TimeoutException:
            return error_response(
                service=SERVICE_NAME,
                status_code=504,
                code="inventory_timeout",
                message="The inventory service did not respond in time",
            )
        except httpx.RequestError:
            return error_response(
                service=SERVICE_NAME,
                status_code=502,
                code="inventory_unreachable",
                message="The inventory service could not be reached",
            )

        if inventory_http_response.status_code != 200:
            return _map_inventory_error(inventory_http_response)

        try:
            inventory = InventoryCheckResponse.model_validate(
                inventory_http_response.json()
            )
        except (ValueError, ValidationError):
            return error_response(
                service=SERVICE_NAME,
                status_code=502,
                code="invalid_inventory_response",
                message="The inventory service returned an invalid response",
            )

        if not inventory.data.available:
            return error_response(
                service=SERVICE_NAME,
                status_code=409,
                code="out_of_stock",
                message="The requested quantity is not available",
            )

        return CheckoutResponse(
            data=CheckoutResult(
                product_id=request.product_id,
                quantity=request.quantity,
            )
        )

    return application


def _map_inventory_error(response: httpx.Response) -> JSONResponse:
    status_mapping = {
        400: (400, "inventory_request_rejected", "Inventory rejected the request"),
        404: (404, "product_not_found", "The requested product does not exist"),
        503: (503, "inventory_unavailable", "The inventory service is unavailable"),
    }
    status_code, code, message = status_mapping.get(
        response.status_code,
        (502, "inventory_bad_gateway", "The inventory service returned an error"),
    )
    return error_response(
        service=SERVICE_NAME,
        status_code=status_code,
        code=code,
        message=message,
    )


app = create_app()
