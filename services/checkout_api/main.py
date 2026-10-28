"""Checkout API application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import Span, Status, StatusCode, Tracer
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
from shared.telemetry import (
    SERVICE_VERSION,
    TelemetryRuntime,
    initialize_telemetry,
    instrument_fastapi_app,
)

SERVICE_NAME = "checkout-api"
HttpClientFactory = Callable[[Settings], httpx.AsyncClient]
TelemetryFactory = Callable[[Settings], TelemetryRuntime]


def _default_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.inventory_api_url,
        timeout=httpx.Timeout(settings.inventory_http_timeout_seconds),
    )


def _default_telemetry(settings: Settings) -> TelemetryRuntime:
    return initialize_telemetry(settings, SERVICE_NAME)


def create_app(
    settings: Settings | None = None,
    http_client_factory: HttpClientFactory = _default_http_client,
    telemetry_factory: TelemetryFactory = _default_telemetry,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        telemetry = telemetry_factory(runtime_settings)
        application.state.tracer = telemetry.tracer
        application.state.inventory_client = http_client_factory(runtime_settings)
        HTTPXClientInstrumentor.instrument_client(
            application.state.inventory_client,
            tracer_provider=telemetry.provider,
        )
        try:
            yield
        finally:
            HTTPXClientInstrumentor.uninstrument_client(
                application.state.inventory_client
            )
            try:
                await application.state.inventory_client.aclose()
            finally:
                telemetry.shutdown()

    application = FastAPI(
        title="Checkout API",
        version=SERVICE_VERSION,
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
        tracer: Tracer = http_request.app.state.tracer

        with tracer.start_as_current_span(
            "checkout.process",
            attributes={
                "checkout.quantity": request.quantity,
                "checkout.scenario": request.scenario.value,
            },
        ) as span:
            try:
                inventory_http_response = await client.post(
                    "/check", json=request.model_dump(mode="json")
                )
            except httpx.TimeoutException as exc:
                span.record_exception(exc)
                _record_outcome(span, "inventory_timeout", 504)
                return error_response(
                    service=SERVICE_NAME,
                    status_code=504,
                    code="inventory_timeout",
                    message="The inventory service did not respond in time",
                )
            except httpx.RequestError as exc:
                span.record_exception(exc)
                _record_outcome(span, "inventory_unreachable", 502)
                return error_response(
                    service=SERVICE_NAME,
                    status_code=502,
                    code="inventory_unreachable",
                    message="The inventory service could not be reached",
                )

            span.set_attribute(
                "checkout.inventory.status_code", inventory_http_response.status_code
            )
            if inventory_http_response.status_code != 200:
                mapped_response = _map_inventory_error(inventory_http_response)
                _record_outcome(
                    span,
                    _inventory_error_outcome(inventory_http_response.status_code),
                    mapped_response.status_code,
                )
                return mapped_response

            try:
                inventory = InventoryCheckResponse.model_validate(
                    inventory_http_response.json()
                )
            except (ValueError, ValidationError):
                _record_outcome(span, "invalid_inventory_response", 502)
                return error_response(
                    service=SERVICE_NAME,
                    status_code=502,
                    code="invalid_inventory_response",
                    message="The inventory service returned an invalid response",
                )

            span.set_attribute("checkout.inventory.available", inventory.data.available)
            if not inventory.data.available:
                _record_outcome(span, "out_of_stock", 409)
                return error_response(
                    service=SERVICE_NAME,
                    status_code=409,
                    code="out_of_stock",
                    message="The requested quantity is not available",
                )

            _record_outcome(span, "accepted", 200)
            return CheckoutResponse(
                data=CheckoutResult(
                    product_id=request.product_id,
                    quantity=request.quantity,
                )
            )

    instrument_fastapi_app(application)
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


def _inventory_error_outcome(status_code: int) -> str:
    return {
        400: "inventory_request_rejected",
        404: "product_not_found",
        503: "inventory_unavailable",
    }.get(status_code, "inventory_bad_gateway")


def _record_outcome(span: Span, outcome: str, status_code: int) -> None:
    span.set_attribute("checkout.outcome", outcome)
    span.set_attribute("checkout.response.status_code", status_code)
    if status_code >= 500:
        span.set_status(Status(StatusCode.ERROR))


app = create_app()
