"""Typed request and response contracts shared by both APIs."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scenario(str, Enum):
    NORMAL = "normal"
    SLOW = "slow"
    ERROR = "error"
    OUT_OF_STOCK = "out-of-stock"


class InventoryCheckRequest(StrictModel):
    product_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=1_000)
    scenario: Scenario = Scenario.NORMAL


class CheckoutRequest(InventoryCheckRequest):
    pass


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    service: str


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    status: Literal["error"] = "error"
    service: str
    error: ErrorDetail


class InventoryAvailability(StrictModel):
    product_id: str
    requested_quantity: int
    available: bool
    available_stock: int


class InventoryCheckResponse(StrictModel):
    status: Literal["ok"] = "ok"
    service: Literal["inventory-api"] = "inventory-api"
    data: InventoryAvailability


class CheckoutResult(StrictModel):
    checkout_status: Literal["accepted"] = "accepted"
    product_id: str
    quantity: int


class CheckoutResponse(StrictModel):
    status: Literal["ok"] = "ok"
    service: Literal["checkout-api"] = "checkout-api"
    data: CheckoutResult
