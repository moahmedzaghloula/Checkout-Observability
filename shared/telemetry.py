"""Shared explicit OpenTelemetry tracing setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from shared.config import Settings


SERVICE_VERSION = "0.1.0"
HEALTH_EXCLUDED_URLS = r"/health$"


@dataclass(slots=True)
class TelemetryRuntime:
    """Own the process-wide trace provider and its orderly shutdown."""

    service_name: str
    provider: TracerProvider
    tracer: Tracer
    _shutdown_lock: Lock = field(default_factory=Lock)
    _is_shutdown: bool = False

    def shutdown(self) -> None:
        """Flush queued spans once, then release exporter resources."""

        with self._shutdown_lock:
            if self._is_shutdown:
                return
            self.provider.force_flush(timeout_millis=5_000)
            self.provider.shutdown()
            self._is_shutdown = True


_runtime: TelemetryRuntime | None = None
_runtime_lock = Lock()


def build_resource(settings: Settings, service_name: str) -> Resource:
    """Create the stable identity attached to every service span."""

    return Resource(
        {
            "service.name": service_name,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": settings.app_env,
        }
    )


def initialize_telemetry(settings: Settings, service_name: str) -> TelemetryRuntime:
    """Initialize and return the single trace provider for this process."""

    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            if _runtime.service_name != service_name:
                raise RuntimeError(
                    "A trace provider is already initialized for another service"
                )
            return _runtime

        set_global_textmap(TraceContextTextMapPropagator())
        provider = TracerProvider(
            resource=build_resource(settings, service_name),
            shutdown_on_exit=False,
        )
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_traces_endpoint,
            timeout=5.0,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=2_048,
                schedule_delay_millis=500,
                max_export_batch_size=512,
                export_timeout_millis=5_000,
            )
        )
        trace.set_tracer_provider(provider)

        _runtime = TelemetryRuntime(
            service_name=service_name,
            provider=provider,
            tracer=provider.get_tracer(f"{service_name}.application", SERVICE_VERSION),
        )
        return _runtime


def instrument_fastapi_app(application: FastAPI) -> None:
    """Add one inbound HTTP tracing middleware without capturing headers."""

    FastAPIInstrumentor.instrument_app(
        application,
        excluded_urls=HEALTH_EXCLUDED_URLS,
        http_capture_headers_server_request=[],
        http_capture_headers_server_response=[],
        exclude_spans=["receive", "send"],
    )
