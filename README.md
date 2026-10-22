# Checkout Observability

Checkout Observability is a small Python service pair designed as a practical foundation for reliability and observability work. The public `checkout-api` validates a checkout request and asks `inventory-api` whether a product is available. Inventory is held in a fixed in-memory catalog, and checks never decrement stock, so repeated requests are deterministic.

## Architecture

- `checkout-api` exposes `GET /health` and `POST /checkout` on port `8000`.
- `inventory-api` exposes `GET /health` and `POST /check` on port `8001`.
- The checkout service uses one lifespan-managed asynchronous HTTP client to call the inventory service.
- Both APIs return a consistent success or error envelope and publish interactive OpenAPI documentation at `/docs`.

## Requirements

- Python 3.11 through 3.14; Python 3.13 is used by CI and the lock file.

## Setup

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

Submit a checkout:

```bash
curl --request POST http://127.0.0.1:8000/checkout \
  --header 'content-type: application/json' \
  --data '{"product_id":"SKU-001","quantity":2,"scenario":"normal"}'
```

Supported scenarios are `normal`, `slow`, `error`, and `out-of-stock`. The `slow` and `error` scenarios are deliberately unavailable by default. To use them locally, start both services with `APP_ENV=local` and `ENABLE_LOCAL_SIMULATIONS=true`. Never enable simulated failures in a shared environment.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | Runtime environment; must be `local` for simulations. |
| `INVENTORY_API_URL` | `http://127.0.0.1:8001` | Inventory service base URL used by checkout. |
| `INVENTORY_HTTP_TIMEOUT_SECONDS` | `2.0` | Checkout-to-inventory HTTP timeout. |
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

The tests exercise validation, deterministic inventory behavior, local-only simulations, service-to-service calls, error mapping, timeout handling, and HTTP client shutdown.
