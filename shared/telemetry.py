"""Shared explicit OpenTelemetry tracing and metrics setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.metrics import Meter

from shared.config import Settings


SERVICE_VERSION = "0.1.0"
HEALTH_EXCLUDED_URLS = r"/health$"


@dataclass(slots=True)
class TelemetryRuntime:
    """Own process-wide telemetry providers and their orderly shutdown."""

    service_name: str
    provider: TracerProvider
    tracer: Tracer
    meter_provider: MeterProvider
    meter: Meter
    _shutdown_lock: Lock = field(default_factory=Lock)
    _is_shutdown: bool = False

    def shutdown(self) -> None:
        """Flush queued telemetry once, then release exporter resources."""

        with self._shutdown_lock:
            if self._is_shutdown:
                return
            self.provider.force_flush(timeout_millis=5_000)
            self.meter_provider.force_flush(timeout_millis=5_000)
            self.provider.shutdown()
            self.meter_provider.shutdown(timeout_millis=5_000)
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
    """Initialize and return the single telemetry runtime for this process."""

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

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=settings.otel_metrics_endpoint,
                timeout=5.0,
            ),
            export_interval_millis=5_000,
            export_timeout_millis=5_000,
        )
        meter_provider = MeterProvider(
            resource=build_resource(settings, service_name),
            metric_readers=[metric_reader],
            shutdown_on_exit=False,
        )
        metrics.set_meter_provider(meter_provider)

        _runtime = TelemetryRuntime(
            service_name=service_name,
            provider=provider,
            tracer=provider.get_tracer(f"{service_name}.application", SERVICE_VERSION),
            meter_provider=meter_provider,
            meter=meter_provider.get_meter(
                f"{service_name}.application", SERVICE_VERSION
            ),
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
