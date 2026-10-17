"""Stateless Streamable-HTTP MCP endpoint for remote connectors (claude.ai).

No authentication: the connector is added with "No sign in". POST /mcp answers
with a single JSON-RPC object (Content-Type: application/json); GET /mcp is
405 because the server never streams SSE.

The two tools mirror the stdio plugin: `quote_warranty` quotes a target URL and
`call_warranty` returns the unsigned PayBox x402 proposal for that call.
"""

import json
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from config import settings

SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_INFO = {"name": "x402-warranty", "version": "0.1.0"}
INSTRUCTIONS = (
    "Insurance for paid x402 API calls. Use quote_warranty to price a call, "
    "then call_warranty to get the PayBox payment proposal that covers it."
)

# 1.5x the tier cap, covering the maximum dynamic premium (38% of upstream).
TIER_MAX_AMOUNT = {"tier1": "75000", "tier2": "375000", "tier3": "1500000"}
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

QUOTE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": ["tier1", "tier2", "tier3"]},
        "network": {"type": "string"},
        "warranted": {"type": "boolean"},
        "upstream_price_atomic": {"type": "string"},
        "premium_atomic": {"type": "string"},
        "warranty_price_atomic": {"type": "string"},
        "upstream_url": {"type": "string"},
        "call_url": {"type": "string"},
        "host": {"type": "string"},
        "host_stats": {"type": "object"},
        "reason": {"type": ["string", "null"]},
    },
    "required": [
        "tier",
        "network",
        "warranted",
        "upstream_price_atomic",
        "premium_atomic",
        "warranty_price_atomic",
        "upstream_url",
        "call_url",
        "host",
        "host_stats",
    ],
    "additionalProperties": False,
}

X402_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "version": {"type": "string", "const": "1"},
        "expiresAt": {"type": "string"},
        "kind": {"type": "string", "const": "x402"},
        "chain": {"type": "string"},
        "request": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"]},
                "body": {},
            },
            "required": ["url", "method"],
            "additionalProperties": False,
        },
        "payment": {
            "type": "object",
            "properties": {"asset": {"type": "string"}, "maxAmount": {"type": "string"}},
            "required": ["asset", "maxAmount"],
            "additionalProperties": False,
        },
    },
    "required": ["version", "expiresAt", "kind", "chain", "request", "payment"],
    "additionalProperties": False,
}

CALL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"io.paybox/transactionProposal": X402_PROPOSAL_SCHEMA},
    "required": ["io.paybox/transactionProposal"],
    "additionalProperties": False,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "quote_warranty",
        "title": "Quote warranty call",
        "description": "Get the warranty tier and price for a target upstream URL without paying.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri", "maxLength": 2048},
                "method": {"type": "string", "enum": ["GET", "POST"]},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "outputSchema": QUOTE_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "call_warranty",
        "title": "Call an upstream API through x402 Warranty",
        "description": (
            "Proposes a paid x402 request to the warranty proxy, which calls the "
            "upstream and refunds if the response is junk."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "format": "uri", "maxLength": 2048},
                "method": {"type": "string", "enum": ["GET", "POST"]},
                "body": {},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "expect": {
                    "type": "object",
                    "properties": {
                        "json_schema": {"type": "object"},
                        "must_contain": {"type": "array", "items": {"type": "string"}},
                        "content_type": {"type": "string"},
                        "min_bytes": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                "max_price_usd": {"type": "string"},
                "tier": {"type": "string", "enum": ["tier1", "tier2", "tier3"]},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "outputSchema": CALL_OUTPUT_SCHEMA,
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _expiry() -> str:
    """Whole-second UTC timestamp within the next five minutes (PayBox rule)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 240))


def _base_url() -> str:
    return settings.public_url.rstrip("/") if settings.public_url else f"http://{settings.host}:{settings.port}"


def _error(request_id: Any, exc: ProtocolError) -> dict:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_text(text: str, structured: dict | None = None, is_error: bool = False) -> dict:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        result["structuredContent"] = structured
    if is_error:
        result["isError"] = True
    return result


async def _tool_quote(arguments: dict) -> dict:
    from main import quote_payload

    url = arguments.get("url")
    if not isinstance(url, str):
        raise ProtocolError(-32602, "Invalid params: 'url' is required")
    method = arguments.get("method") or "GET"
    if method not in {"GET", "POST"}:
        raise ProtocolError(-32602, "Invalid params: 'method' must be GET or POST")

    body, status = await quote_payload(url, method)
    if status != 200:
        return _tool_text(f"Warranty quote rejected: {body.get('reason', 'unknown reason')}", is_error=True)
    return _tool_text(
        f"Tier: {body['tier']}, upstream: {body['upstream_price_atomic']}, "
        f"warranty: {body['warranty_price_atomic']} (network {body['network']})",
        structured=body,
    )


async def _tool_call(arguments: dict) -> dict:
    method = arguments.get("method") or "GET"
    if method not in {"GET", "POST"}:
        raise ProtocolError(-32602, "Invalid params: 'method' must be GET or POST")
    url = arguments.get("url")
    if not isinstance(url, str):
        raise ProtocolError(-32602, "Invalid params: 'url' is required")

    query = f"?url={quote(url, safe='')}&method={method}"
    tier = arguments.get("tier")
    if tier:
        if tier not in TIER_MAX_AMOUNT:
            raise ProtocolError(-32602, "Invalid params: 'tier' must be tier1, tier2 or tier3")
        # Tier fallback avoids a network round-trip and keeps the tool usable
        # when the upstream host is cold.
        call_url = f"{_base_url()}/v1/warranty/call/{tier}{query}"
        max_amount = TIER_MAX_AMOUNT[tier]
        chain = settings.x402_network
    else:
        from main import quote_payload

        body, status = await quote_payload(url, method)
        if status != 200:
            return _tool_text(f"Warranty quote rejected: {body.get('reason', 'unknown reason')}", is_error=True)
        if not body.get("warranted"):
            return _tool_text(f"Warranty not offered: {body.get('reason') or 'unknown reason'}", is_error=True)
        # call_url already carries ?url=...&method=... for the price callback.
        call_url = body["call_url"]
        max_amount = body["warranty_price_atomic"]
        chain = body["network"]

    payload: dict[str, Any] = {
        "url": url,
        "method": method,
        "headers": arguments.get("headers") or {},
        "expect": arguments.get("expect") or {},
        "max_price_usd": arguments.get("max_price_usd") or "1.00",
    }
    if arguments.get("body") is not None:
        payload["body"] = arguments["body"]

    proposal = {
        "version": "1",
        "expiresAt": _expiry(),
        "kind": "x402",
        "chain": chain,
        "request": {"url": call_url, "method": "POST", "body": payload},
        "payment": {"asset": BASE_USDC, "maxAmount": str(max_amount)},
    }
    if len(json.dumps(payload)) > 64 * 1024:
        raise ProtocolError(-32602, "Invalid params: x402 JSON body exceeds 64 KiB")
    return _tool_text(
        "Unsigned proposal prepared. PayBox must validate and obtain any required "
        "approval; nothing has been signed or paid.",
        structured={"io.paybox/transactionProposal": proposal},
    )


async def _dispatch(message: dict) -> dict | Response:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method is None:
        # JSON-RPC response from the client — nothing to answer.
        return Response(status_code=202)

    if str(method).startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
        )

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name") if isinstance(params, dict) else None
        arguments = (params.get("arguments") or {}) if isinstance(params, dict) else {}
        if not isinstance(arguments, dict):
            raise ProtocolError(-32602, "Invalid params: 'arguments' must be an object")
        if name == "quote_warranty":
            return _result(request_id, await _tool_quote(arguments))
        if name == "call_warranty":
            return _result(request_id, await _tool_call(arguments))
        raise ProtocolError(-32602, f"Unknown tool: {name}")

    raise ProtocolError(-32601, f"Method not found: {method}")


router = APIRouter()


async def _handle(request: Request) -> Response:
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    try:
        if isinstance(raw, list):
            if not raw:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}},
                    status_code=400,
                )
            responses = []
            for message in raw:
                out = await _dispatch(message)
                if isinstance(out, dict):
                    responses.append(out)
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses)
        out = await _dispatch(raw)
    except ProtocolError as exc:
        return JSONResponse(_error(raw.get("id") if isinstance(raw, dict) else None, exc))
    except Exception as exc:  # pragma: no cover - defensive
        return JSONResponse(_error(None, ProtocolError(-32603, f"Internal error: {exc}")))

    if isinstance(out, Response):
        return out
    return JSONResponse(out)


@router.post("/mcp")
@router.post("/mcp/")
async def mcp_post(request: Request):
    return await _handle(request)


@router.get("/mcp")
@router.get("/mcp/")
async def mcp_get():
    # No SSE stream: the server only answers POSTed JSON-RPC.
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
@router.delete("/mcp/")
async def mcp_delete():
    return Response(status_code=405, headers={"Allow": "POST"})
