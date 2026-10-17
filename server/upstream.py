"""Upstream x402 buyer client."""

import base64
import json
from typing import Any

import httpx
from eth_account import Account

from x402 import x402Client
from x402.http import x402HTTPClient
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.schemas import PaymentRequired

from config import settings


class UpstreamClient:
    def __init__(self) -> None:
        self.client = x402Client()
        if not settings.upstream_float_private_key:
            raise ValueError("UPSTREAM_FLOAT_PRIVATE_KEY not set")
        account = Account.from_key(settings.upstream_float_private_key)
        register_exact_evm_client(self.client, EthAccountSigner(account))
        self.http_client = x402HTTPClient(self.client)

    async def get_quote(self, url: str, method: str = "GET", headers: dict | None = None, body: Any | None = None) -> dict:
        """Make an unpaid probe request and return the 402 requirements."""
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
            try:
                response = await http.request(method, url, headers=headers, json=body)
            except Exception as exc:
                raise UpstreamError(f"Upstream probe failed: {exc}")
        if response.status_code != 402:
            raise UpstreamError(f"Upstream did not require payment: HTTP {response.status_code}")
        raw = response.headers.get("PAYMENT-REQUIRED")
        if not raw:
            raise UpstreamError("Upstream 402 missing PAYMENT-REQUIRED header")
        try:
            decoded = json.loads(base64.b64decode(raw).decode("utf-8"))
            return PaymentRequired.model_validate(decoded).model_dump()
        except Exception as exc:
            raise UpstreamError(f"Invalid PAYMENT-REQUIRED header: {exc}")

    async def call(self, url: str, method: str = "GET", headers: dict | None = None, body: Any | None = None) -> dict:
        """Make a paid request to an x402 upstream and return response."""
        async with x402HttpxClient(self.client) as http:
            if method.upper() == "GET":
                response = await http.get(url, headers=headers)
            else:
                response = await http.post(url, json=body, headers=headers)
            await response.aread()
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
            "payment_status": self.http_client.get_payment_settle_response(lambda name: response.headers.get(name)),
        }


class UpstreamError(Exception):
    pass
