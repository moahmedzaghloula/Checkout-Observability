"""Explicit OpenTelemetry tracing setup for checkout-api."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer

from shared.config import Settings


SERVICE_NAME = "checkout-api"
SERVICE_VERSION = "0.1.0"
TRACER_NAME = "checkout-api.checkout"


@dataclass(slots=True)
class TelemetryRuntime:
    """Own the process-wide trace provider and its orderly shutdown."""

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


def build_resource(settings: Settings) -> Resource:
    """Create the stable identity attached to every checkout span."""

    return Resource(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment.name": settings.app_env,
        }
    )


def initialize_telemetry(settings: Settings) -> TelemetryRuntime:
    """Initialize and return the single trace provider for this process."""

    global _runtime

    with _runtime_lock:
        if _runtime is not None:
            return _runtime

        provider = TracerProvider(
            resource=build_resource(settings),
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
            provider=provider,
            tracer=provider.get_tracer(TRACER_NAME, SERVICE_VERSION),
        )
        return _runtime
