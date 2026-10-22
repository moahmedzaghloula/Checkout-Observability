"""Consistent API error responses."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from shared.models import ErrorDetail, ErrorResponse


def error_response(
    *, service: str, status_code: int, code: str, message: str
) -> JSONResponse:
    payload = ErrorResponse(
        service=service,
        error=ErrorDetail(code=code, message=message),
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def install_validation_handler(app: FastAPI, service: str) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            service=service,
            status_code=422,
            code="validation_error",
            message="Request validation failed",
        )
