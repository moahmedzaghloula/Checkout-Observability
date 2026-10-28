# Checkout Observability

Checkout Observability is a small Python service pair designed as a practical foundation for reliability and observability work. The public `checkout-api` validates a checkout request and asks `inventory-api` whether a product is available. Inventory is held in a fixed in-memory catalog, and checks never decrement stock, so repeated requests are deterministic.

## Architecture

- `checkout-api` exposes `GET /health` and `POST /checkout` on port `8000`.
- `inventory-api` exposes `GET /health` and `POST /check` on port `8001`.
- The checkout service uses one lifespan-managed asynchronous HTTP client to call the inventory service.
- Both services export traces and metrics over OTLP/HTTP to an OpenTelemetry Collector. The Collector forwards traces to Jaeger and exposes received application metrics for Prometheus to scrape.
- Prometheus stores time-series data, while Grafana is provisioned with Prometheus and Jaeger data sources plus a checkout dashboard.
- Application events are written as structured JSON to standard output with active trace and span correlation.
- Both APIs return a consistent success or error envelope and publish interactive OpenAPI documentation at `/docs`.

## Requirements

- Python 3.11 through 3.14; Python 3.13 is used by CI and the lock file.
- Docker Engine with Docker Compose v2 for the containerized local environment.

## Containerized local development

Generate a local Grafana password without printing it, then build the images and wait for all services to become healthy:

```bash
install -d -m 700 .local-secrets
umask 077
openssl rand -hex 32 | tr -d '\n' > .local-secrets/grafana_admin_password
docker compose up --build --wait
```

Browser and API ports bind only to localhost:

- Checkout API: `http://127.0.0.1:8000`
- Grafana: `http://127.0.0.1:3000`
- Prometheus: `http://127.0.0.1:9090`
- Jaeger: `http://127.0.0.1:16686`

Sign in to Grafana as `admin` with the password stored in the ignored `.local-secrets/grafana_admin_password` file. Inventory, the Collector's OTLP receiver, and its Prometheus exporter remain internal to the Compose network. Grafana, Prometheus, and the applications reach one another through Compose service names rather than `localhost`.

Submit a checkout:

```bash
curl --request POST http://127.0.0.1:8000/checkout \
  --header 'content-type: application/json' \
  --data '{"product_id":"SKU-001","quantity":2,"scenario":"normal"}'
```

Stop and remove only this Compose application's containers and networks:

```bash
docker compose down
```

The Compose configuration is intended for local development and explicitly enables the `slow` and `error` simulations with `APP_ENV=local`. Configuration mounts use the private `Z` SELinux label required by the Fedora target. Local telemetry hops use plaintext inside the isolated Compose network. Prometheus and Grafana use named volumes; Jaeger still uses transient in-memory storage, so traces are lost when its container stops. This setup is not a production security or durability design.

## Tracing

FastAPI and httpx instrumentation are configured programmatically. W3C Trace Context propagation connects the checkout server span, manual `checkout.process` span, outbound HTTP client span, inventory server span, and manual `inventory.lookup` span in one trace. Health endpoints are excluded, and span attributes are bounded and exclude request bodies, product IDs, headers, and credentials. Do not add the OpenTelemetry zero-code agent on top of this explicit setup because it would duplicate instrumentation.

Each service uses a distinct `service.name` with the same `service.version` and `deployment.environment.name` resource attributes.

Use the Jaeger UI at `http://127.0.0.1:16686` and select `checkout-api` after submitting a checkout. The local stack pins OpenTelemetry Collector `0.162.0` and Jaeger `2.22.0` by version and image digest. Python tracing packages are pinned at `1.45.1`; FastAPI and httpx instrumentation packages are pinned at `0.66b1`.

## Metrics and dashboard

The checkout service records `checkout.requests` as a counter and `checkout.request.duration` as an explicit-bucket histogram measured in seconds. A request is included exactly once only after FastAPI validation succeeds and the `/checkout` handler starts; validation responses produced before the handler, including `422`, are excluded. Every included outcome is counted and timed. Only `accepted` has `result="success"`; out of stock, inventory rejection, missing products, invalid upstream responses, timeouts, connection failures, and upstream errors have `result="error"`.

Metric attributes are limited to the bounded `outcome` and `result` values. Product, customer, order, trace, and span identifiers, request bodies, and raw URLs are never metric labels. The Collector's `prometheus/application` exporter exposes these received application metrics on its internal port `8889`; this is separate from the Collector's own internal operational metrics.

The provisioned `Checkout Observability` dashboard includes:

- request rate from `sum(rate(checkout_requests_total[$__rate_interval]))`, in requests per second;
- error ratio from the rate of `result="error"` divided by the total request rate;
- P95 latency from the actual `checkout_request_duration_seconds_bucket` histogram.

The Prometheus exporter in Collector `0.162.0` is a beta metrics component. Dashboard and data source provisioning is automatic; no manual Grafana UI changes are required. Grafana `13.3.0` and Prometheus `3.15.1` are pinned by version and image digest.

## Structured logs

Both applications use Python logging to emit one-line JSON events to standard output. Each event includes an RFC 3339 timestamp, severity, service identity, bounded outcome fields, duration in seconds, and the active OpenTelemetry trace and span IDs. Records emitted outside an active span contain null correlation IDs, preventing context from leaking between requests. Request bodies, authorization headers, credentials, and environment dumps are not logged.

Use `docker compose logs checkout-api` or `docker compose logs inventory-api` for local inspection. A log's `trace_id` can be pasted into Jaeger to locate the connected trace. These correlated stdout records are not an OTLP logs pipeline and are not durable log storage; no Loki or other log backend is included.

## Native Python development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --requirement requirements.lock
```

Run each service from the repository root in a separate terminal:

```bash
.venv/bin/python -m uvicorn services.inventory_api.main:app --port 8001
```

```bash
.venv/bin/python -m uvicorn services.checkout_api.main:app --port 8000
```

Supported scenarios are `normal`, `slow`, `error`, and `out-of-stock`. The `slow` and `error` scenarios are deliberately unavailable by default. To use them locally, start both services with `APP_ENV=local` and `ENABLE_LOCAL_SIMULATIONS=true`. Never enable simulated failures in a shared environment.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Runtime environment; must be `local` for simulations. |
| `INVENTORY_API_URL` | `http://127.0.0.1:8001` | Inventory service base URL used by checkout. |
| `INVENTORY_HTTP_TIMEOUT_SECONDS` | `2.0` | Checkout-to-inventory HTTP timeout. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `http://127.0.0.1:4318/v1/traces` | Full OTLP/HTTP endpoint used by both services for spans. |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | `http://127.0.0.1:4318/v1/metrics` | Full OTLP/HTTP endpoint used by both services for metrics. |
| `GRAFANA_ADMIN_USER` | `admin` | Local Grafana administrator name used by Compose. |
| `GRAFANA_ADMIN_PASSWORD_FILE` | `./.local-secrets/grafana_admin_password` | Ignored local password file consumed through Grafana's secret-file setting. |
| `ENABLE_LOCAL_SIMULATIONS` | `false` | Enables slow and error scenarios only when `APP_ENV=local`. |
| `SIMULATED_DELAY_SECONDS` | `1.0` | Delay applied to the local slow scenario. |

`.env.example` contains safe example values. The application reads environment variables directly and does not load `.env` files automatically.

## API behavior

- `POST /check` returns `200` with `available: true` or `false`; unknown products return `404`.
- `POST /checkout` returns `200` when inventory is available and `409` when it is not.
- Invalid request bodies return `422`, upstream connection problems return `502`, unavailable inventory returns `503`, and inventory timeouts return `504`.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests exercise validation, deterministic inventory behavior, local-only simulations, service-to-service calls, error mapping, timeout handling, HTTP client shutdown, telemetry lifecycle, parent-child relationships, exception recording, bounded metric measurements, structured log correlation, and context cleanup.
