FROM python:3.13.16-slim-bookworm@sha256:73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.runtime.lock ./
RUN python -m pip install \
    --no-cache-dir \
    --root-user-action=ignore \
    --requirement requirements.runtime.lock

COPY shared/ ./shared/
COPY services/__init__.py ./services/__init__.py
COPY services/checkout_api/ ./services/checkout_api/

USER app:app

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "services.checkout_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
