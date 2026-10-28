# Checkout Observability

Checkout Observability is a small Python service pair designed as a practical foundation for reliability and observability work. The public `checkout-api` validates a checkout request and asks `inventory-api` whether a product is available. Inventory is held in a fixed in-memory catalog, and checks never decrement stock, so repeated requests are deterministic.

## Architecture

- `checkout-api` exposes `GET /health` and `POST /checkout` on port `8000`.
- `inventory-api` exposes `GET /health` and `POST /check` on port `8001`.
- The checkout service uses one lifespan-managed asynchronous HTTP client to call the inventory service.
- Both services export traces over OTLP/HTTP to an OpenTelemetry Collector, which batches and forwards them to Jaeger over OTLP/gRPC.
- Both APIs return a consistent success or error envelope and publish interactive OpenAPI documentation at `/docs`.

## Requirements

- Python 3.11 through 3.14; Python 3.13 is used by CI and the lock file.
- Docker Engine with Docker Compose v2 for the containerized local environment.

## Containerized local development

Build the images and wait for all services to become healthy:

```bash
docker compose up --build --wait
```

Only `checkout-api` and the Jaeger UI are published to the host, at `127.0.0.1:8000` and `http://127.0.0.1:16686` respectively. Inventory and both OTLP ports remain internal to the Compose network. The checkout service reaches them through the `inventory-api` and `otel-collector` Compose service names.

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

The Compose configuration is intended for local development and explicitly enables the `slow` and `error` simulations with `APP_ENV=local`. The Collector configuration is mounted read-only with the private `Z` SELinux label required by the Fedora target. Local telemetry hops use plaintext inside the isolated Compose network. Jaeger uses transient in-memory storage, so traces are lost when its container stops; this setup is not a production security or durability design.

## Tracing

FastAPI and httpx instrumentation are configured programmatically. W3C Trace Context propagation connects the checkout server span, manual `checkout.process` span, outbound HTTP client span, inventory server span, and manual `inventory.lookup` span in one trace. Health endpoints are excluded, and span attributes are bounded and exclude request bodies, product IDs, headers, and credentials. Do not add the OpenTelemetry zero-code agent on top of this explicit setup because it would duplicate instrumentation.

Each service uses a distinct `service.name` with the same `service.version` and `deployment.environment.name` resource attributes. Metrics and OpenTelemetry log export are not configured.

Use the Jaeger UI at `http://127.0.0.1:16686` and select `checkout-api` after submitting a checkout. The local stack pins OpenTelemetry Collector `0.162.0` and Jaeger `2.22.0` by version and image digest. Python tracing packages are pinned at `1.45.1`; FastAPI and httpx instrumentation packages are pinned at `0.66b1`.

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

The tests exercise validation, deterministic inventory behavior, local-only simulations, service-to-service calls, error mapping, timeout handling, HTTP client shutdown, telemetry lifecycle, parent-child relationships, exception recording, and span metadata.
