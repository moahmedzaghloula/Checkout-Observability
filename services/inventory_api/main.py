"""Inventory API application."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import logging
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from shared.config import Settings
from shared.errors import error_response, install_validation_handler
from shared.logging import configure_application_logging
from shared.models import (
    ErrorResponse,
    HealthResponse,
    InventoryAvailability,
    InventoryCheckRequest,
    InventoryCheckResponse,
    Scenario,
)
from shared.telemetry import (
    SERVICE_VERSION,
    TelemetryRuntime,
    initialize_telemetry,
    instrument_fastapi_app,
)


SERVICE_NAME = "inventory-api"
TelemetryFactory = Callable[[Settings], TelemetryRuntime]
CATALOG: dict[str, int] = {
    "SKU-001": 10,
    "SKU-002": 0,
    "SKU-003": 25,
}


def _default_telemetry(settings: Settings) -> TelemetryRuntime:
    return initialize_telemetry(settings, SERVICE_NAME)


def create_app(
    settings: Settings | None = None,
    telemetry_factory: TelemetryFactory = _default_telemetry,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    application_logger = configure_application_logging(
        SERVICE_NAME, runtime_settings.app_env
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        telemetry = telemetry_factory(runtime_settings)
        application.state.tracer = telemetry.tracer
        application.state.logger = application_logger
        try:
            yield
        finally:
            telemetry.shutdown()

    application = FastAPI(
        title="Inventory API",
        version=SERVICE_VERSION,
        lifespan=lifespan,
    )
    application.state.logger = application_logger
    application.state.tracer = trace.get_tracer(
        f"{SERVICE_NAME}.application", SERVICE_VERSION
    )
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
        http_request: Request,
    ) -> InventoryCheckResponse | JSONResponse:
        tracer: Tracer = http_request.app.state.tracer
        logger: logging.Logger = http_request.app.state.logger
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "inventory.lookup",
            attributes={
                "inventory.quantity": request.quantity,
                "inventory.scenario": request.scenario.value,
            },
        ) as span:
            try:
                stock = CATALOG.get(request.product_id)
                if stock is None:
                    _record_lookup_outcome(
                        span, logger, started_at, "product_not_found", 404
                    )
                    return error_response(
                        service=SERVICE_NAME,
                        status_code=404,
                        code="product_not_found",
                        message="The requested product does not exist",
                    )

                if request.scenario in {Scenario.SLOW, Scenario.ERROR}:
                    if not runtime_settings.enable_local_simulations:
                        _record_lookup_outcome(
                            span, logger, started_at, "simulation_disabled", 400
                        )
                        return error_response(
                            service=SERVICE_NAME,
                            status_code=400,
                            code="simulation_disabled",
                            message="Slow and error scenarios are disabled",
                        )

                if request.scenario is Scenario.SLOW:
                    await asyncio.sleep(runtime_settings.simulated_delay_seconds)
                elif request.scenario is Scenario.ERROR:
                    _record_lookup_outcome(
                        span, logger, started_at, "simulated_failure", 503
                    )
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
                span.set_attribute("inventory.available", available)
                _record_lookup_outcome(
                    span,
                    logger,
                    started_at,
                    "available" if available else "out_of_stock",
                    200,
                )
                return InventoryCheckResponse(
                    data=InventoryAvailability(
                        product_id=request.product_id,
                        requested_quantity=request.quantity,
                        available=available,
                        available_stock=stock,
                    )
                )
            except Exception:
                _record_lookup_outcome(
                    span, logger, started_at, "internal_error", 500
                )
                raise

    instrument_fastapi_app(application)
    return application


def _record_lookup_outcome(
    span: Span,
    logger: logging.Logger,
    started_at: float,
    outcome: str,
    status_code: int,
) -> None:
    duration_seconds = max(perf_counter() - started_at, 0.0)
    span.set_attribute("inventory.outcome", outcome)
    span.set_attribute("inventory.response.status_code", status_code)
    if status_code >= 400:
        span.set_status(Status(StatusCode.ERROR))
    logger.info(
        "Inventory request completed",
        extra={
            "event": "inventory.completed",
            "outcome": outcome,
            "result": "success" if status_code < 400 else "error",
            "status_code": status_code,
            "duration_seconds": round(duration_seconds, 6),
        },
    )


app = create_app()
