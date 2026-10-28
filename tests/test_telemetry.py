from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace.sampling import ParentBased
from opentelemetry.sdk.trace.export import SpanExportResult
import pytest

from shared.config import Settings
from shared import telemetry


class StubExporter:
    def export(self, spans):
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis=30_000):
        return True

    def shutdown(self):
        return None


class StubMetricExporter(MetricExporter):
    def __init__(self):
        super().__init__()

    def export(self, metrics_data, timeout_millis=10_000):
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10_000):
        return True

    def shutdown(self, timeout_millis=30_000, **_kwargs):
        return None


def test_initialize_telemetry_creates_one_provider_per_process(monkeypatch) -> None:
    registered_providers = []
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **_kwargs: StubExporter())
    monkeypatch.setattr(
        telemetry, "OTLPMetricExporter", lambda **_kwargs: StubMetricExporter()
    )
    monkeypatch.setattr(
        telemetry.trace,
        "set_tracer_provider",
        registered_providers.append,
    )
    registered_meter_providers = []
    monkeypatch.setattr(
        telemetry.metrics,
        "set_meter_provider",
        registered_meter_providers.append,
    )

    first = telemetry.initialize_telemetry(Settings(app_env="test"), "checkout-api")
    second = telemetry.initialize_telemetry(Settings(app_env="different"), "checkout-api")

    assert first is second
    assert registered_providers == [first.provider]
    assert registered_meter_providers == [first.meter_provider]
    assert first.provider.resource.attributes["service.name"] == "checkout-api"
    assert first.provider.resource.attributes["service.version"] == "0.1.0"
    assert first.provider.resource.attributes["deployment.environment.name"] == "test"
    with pytest.raises(RuntimeError, match="another service"):
        telemetry.initialize_telemetry(Settings(app_env="test"), "inventory-api")
    first.shutdown()


def test_settings_reads_explicit_trace_endpoint(monkeypatch) -> None:
    endpoint = "http://collector.test:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)

    assert Settings.from_env().otel_traces_endpoint == endpoint


def test_settings_reads_explicit_metrics_endpoint(monkeypatch) -> None:
    endpoint = "http://collector.test:4318/v1/metrics"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", endpoint)

    assert Settings.from_env().otel_metrics_endpoint == endpoint


def test_trace_provider_reads_parent_based_ratio_sampler_from_environment(
    monkeypatch,
) -> None:
    registered_providers = []
    monkeypatch.setattr(telemetry, "_runtime", None)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.25")
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **_kwargs: StubExporter())
    monkeypatch.setattr(
        telemetry, "OTLPMetricExporter", lambda **_kwargs: StubMetricExporter()
    )
    monkeypatch.setattr(
        telemetry.trace,
        "set_tracer_provider",
        registered_providers.append,
    )
    monkeypatch.setattr(telemetry.metrics, "set_meter_provider", lambda _provider: None)

    runtime = telemetry.initialize_telemetry(Settings(app_env="test"), "checkout-api")

    assert isinstance(runtime.provider.sampler, ParentBased)
    assert "TraceIdRatioBased{0.25}" in runtime.provider.sampler.get_description()
    assert registered_providers == [runtime.provider]
    runtime.shutdown()
