"""Pydantic models for the warranty server."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExpectConfig(BaseModel):
    json_schema: dict[str, Any] | None = Field(default=None, alias="json_schema")
    must_contain: list[str] | None = Field(default=None, alias="must_contain")
    content_type: str | None = Field(default=None, alias="content_type")
    min_bytes: int | None = Field(default=None, alias="min_bytes")

    model_config = {"populate_by_name": True}


class WarrantyCallRequest(BaseModel):
    url: str
    method: Literal["GET", "POST"] = "GET"
    body: Any | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    expect: ExpectConfig = Field(default_factory=ExpectConfig)
    max_price_usd: str | None = None


class HostStats(BaseModel):
    host: str
    calls: int
    refunds: int
    refund_rate: float
    warranted: bool
    reason: str | None = None


class WarrantyQuoteResponse(BaseModel):
    tier: Literal["tier1", "tier2", "tier3"]
    warranted: bool
    upstream_price_atomic: str
    premium_atomic: str
    warranty_price_atomic: str
    upstream_url: str
    call_url: str
    host: str
    host_stats: HostStats
    reason: str | None = None


class WarrantyResult(BaseModel):
    verdict: Literal["pass", "refunded", "error"]
    reason: str | None = None
    refund_tx: str | None = None
    upstream_host: str
    warranty_id: str
    upstream_status: int | None = None


class RefundReason(str, Enum):
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    NON_2XX = "non_2xx"
    EMPTY_BODY = "empty_body"
    INVALID_JSON = "invalid_json"
    UPSTREAM_PAYMENT_FAILED = "upstream_payment_failed"
    INTERNAL_ERROR = "internal_error"
    SCHEMA_MISMATCH = "schema_mismatch"
