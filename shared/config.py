"""Environment-backed application settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _read_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    value = raw_value.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def _read_positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc

    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _validate_http_url(name: str, value: str, *, strip_trailing_slash: bool) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"{name} must not contain credentials, a query, or a fragment"
        )
    return value.rstrip("/") if strip_trailing_slash else value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration shared by the two services."""

    app_env: str = "development"
    inventory_api_url: str = "http://127.0.0.1:8001"
    inventory_http_timeout_seconds: float = 2.0
    otel_traces_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    otel_metrics_endpoint: str = "http://127.0.0.1:4318/v1/metrics"
    enable_local_simulations: bool = False
    simulated_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        normalized_env = self.app_env.strip().lower()
        object.__setattr__(self, "app_env", normalized_env)
        object.__setattr__(
            self,
            "inventory_api_url",
            _validate_http_url(
                "INVENTORY_API_URL",
                self.inventory_api_url.strip(),
                strip_trailing_slash=True,
            ),
        )
        object.__setattr__(
            self,
            "otel_traces_endpoint",
            _validate_http_url(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                self.otel_traces_endpoint.strip(),
                strip_trailing_slash=False,
            ),
        )
        object.__setattr__(
            self,
            "otel_metrics_endpoint",
            _validate_http_url(
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                self.otel_metrics_endpoint.strip(),
                strip_trailing_slash=False,
            ),
        )

        if self.inventory_http_timeout_seconds <= 0:
            raise ValueError("INVENTORY_HTTP_TIMEOUT_SECONDS must be greater than zero")
        if self.simulated_delay_seconds <= 0:
            raise ValueError("SIMULATED_DELAY_SECONDS must be greater than zero")
        if self.enable_local_simulations and normalized_env != "local":
            raise ValueError(
                "ENABLE_LOCAL_SIMULATIONS can only be enabled when APP_ENV=local"
            )

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings without logging their resolved values."""

        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            inventory_api_url=os.getenv(
                "INVENTORY_API_URL", "http://127.0.0.1:8001"
            ),
            inventory_http_timeout_seconds=_read_positive_float(
                "INVENTORY_HTTP_TIMEOUT_SECONDS", 2.0
            ),
            otel_traces_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "http://127.0.0.1:4318/v1/traces",
            ),
            otel_metrics_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
                "http://127.0.0.1:4318/v1/metrics",
            ),
            enable_local_simulations=_read_bool(
                "ENABLE_LOCAL_SIMULATIONS", False
            ),
            simulated_delay_seconds=_read_positive_float(
                "SIMULATED_DELAY_SECONDS", 1.0
            ),
        )
