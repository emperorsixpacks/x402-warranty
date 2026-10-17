"""x402 Warranty proxy server — v3 micro-insurance model."""

import json
from contextlib import asynccontextmanager
from urllib.parse import quote as urlquote
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import HTTPRequestContext, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from config import settings
from ledger import (
    add_accounting_entry,
    compute_premium,
    create_call,
    daily_host_payout,
    get_call,
    get_refund_for_warranty,
    hourly_payer_refunds,
    host_stats,
    init_db,
    is_warranted,
    record_refund,
    stats,
    update_call,
)
from models import WarrantyCallRequest, WarrantyQuoteResponse
from refund import get_treasury_balance, send_refund
from ssrf import SSRFError, validate_url
from upstream import UpstreamClient, UpstreamError
from validation import validate_response


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.x402_server = x402_server
    yield


app = FastAPI(title="x402 Warranty v3", lifespan=lifespan)

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
x402_server = x402ResourceServer(facilitator)
x402_server.register("eip155:84532", ExactEvmServerScheme())

# Shared quote cache so the /v1/warranty/quote response and the x402 middleware
# price callback agree for the lifetime of a payment attempt.
_quote_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_QUOTE_CACHE_TTL_SECONDS = 300


def _upstream_client() -> UpstreamClient:
    return UpstreamClient()


def _atomic_to_money(atomic: int) -> str:
    """Convert atomic USDC (6 decimals) to a Money string the x402 SDK accepts."""
    dollars = atomic // 1_000_000
    cents = atomic % 1_000_000
    return f"${dollars}.{cents:06d}"


def _get_query_url_method(context: HTTPRequestContext) -> tuple[str, str]:
    """Extract the upstream URL and method from the x402 request query string.

    The middleware body adapter is unavailable, so the plugin passes the target
    upstream URL and method as query parameters.
    """
    params = context.adapter.get_query_params() or {}
    url = params.get("url") if isinstance(params.get("url"), str) else None
    if not url:
        raise ValueError("missing 'url' query parameter")
    method = params.get("method") if isinstance(params.get("method"), str) else "GET"
    return url, method.upper()


async def _build_quote(url: str, method: str) -> dict:
    """Return a cached quote dict for the upstream URL/method.

    Keys: tier, warranted, upstream_price_atomic (int), premium_atomic (int),
          warranty_price_atomic (int), upstream_url, host, host_stats, reason, error.
    """
    now = __import__("time").time()
    cache_key = (url, method)
    if cache_key in _quote_cache:
        expires_at, cached = _quote_cache[cache_key]
        if now < expires_at:
            return cached
        del _quote_cache[cache_key]

    validate_url(url)
    parsed = urlparse(url)
    host = parsed.netloc
    hstats = host_stats(host)
    warranted, reason = is_warranted(hstats)

    client = _upstream_client()
    try:
        req = WarrantyCallRequest(url=url, method=method)  # type: ignore[arg-type]
        requirements = await client.get_quote(req.url, req.method, req.headers, req.body)
    except UpstreamError as exc:
        raise ValueError(f"upstream probe failed: {exc}") from exc

    accepts = requirements.get("accepts", [])
    upstream_price = 0
    for opt in accepts:
        if opt.get("scheme") == "exact" and opt.get("network") == settings.x402_network:
            upstream_price = max(upstream_price, int(opt.get("amount", 0)))

    if upstream_price == 0:
        raise ValueError("upstream does not accept our configured network/scheme")

    if upstream_price <= settings.tier1_max_atomic:
        tier = "tier1"
    elif upstream_price <= settings.tier2_max_atomic:
        tier = "tier2"
    elif upstream_price <= settings.tier3_max_atomic:
        tier = "tier3"
    else:
        raise ValueError("upstream price exceeds tier3 cap")

    premium, _ = compute_premium(hstats, upstream_price)
    if not warranted:
        premium = 0

    result = {
        "tier": tier,
        "warranted": warranted,
        "upstream_price_atomic": upstream_price,
        "premium_atomic": premium,
        "warranty_price_atomic": upstream_price + premium,
        "upstream_url": url,
        "host": host,
        "host_stats": hstats,
        "reason": reason,
    }
    _quote_cache[cache_key] = (now + _QUOTE_CACHE_TTL_SECONDS, result)
    return result


def build_routes() -> dict:
    def make_dynamic_price(tier: str):
        async def price(context: HTTPRequestContext) -> str:
            url, method = _get_query_url_method(context)
            quote = await _build_quote(url, method)
            if quote["tier"] != tier:
                # Buyer pinned a lower tier than the upstream price requires.
                # The handler will issue a coverage-breach refund after payment.
                return _atomic_to_money(quote["warranty_price_atomic"])
            return _atomic_to_money(quote["warranty_price_atomic"])

        return price

    routes = {}
    for tier in ["tier1", "tier2", "tier3"]:
        routes[f"POST /v1/warranty/call/{tier}"] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    price=make_dynamic_price(tier),
                    network=settings.x402_network,
                    pay_to=settings.treasury_address,
                )
            ],
            description=f"Warranty call with {tier} coverage",
            mime_type="application/json",
            hook_timeout_seconds=20.0,
        )
    return routes


app.add_middleware(PaymentMiddlewareASGI, routes=build_routes(), server=x402_server)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/warranty/quote")
async def quote(url: str, method: str = "GET"):
    method = method.upper()
    if method not in {"GET", "POST"}:
        return JSONResponse({"tier": "reject", "reason": f"method not allowed: {method}"}, status_code=400)
    try:
        quote = await _build_quote(url, method)
    except SSRFError as exc:
        return JSONResponse({"tier": "reject", "reason": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"tier": "reject", "reason": str(exc)}, status_code=400)

    tier = quote["tier"]
    # Tier-history gate applies at quote time: cold hosts only get tier1 coverage.
    if tier != "tier1" and quote["host_stats"]["calls"] < settings.tier1_host_max_calls:
        if quote["upstream_price_atomic"] <= settings.tier1_max_atomic:
            tier = "tier1"
        else:
            return JSONResponse(
                {"tier": "reject", "reason": f"host needs {settings.tier1_host_max_calls}+ calls for coverage above tier1"},
                status_code=400,
            )

    base = settings.public_url.rstrip("/") if settings.public_url else f"http://{settings.host}:{settings.port}"
    call_url = f"{base}/v1/warranty/call/{tier}?url={urlquote(url, safe='')}&method={method}"

    return WarrantyQuoteResponse(
        tier=tier,
        network=settings.x402_network,
        warranted=quote["warranted"],
        upstream_price_atomic=str(quote["upstream_price_atomic"]),
        premium_atomic=str(quote["premium_atomic"]),
        warranty_price_atomic=str(quote["warranty_price_atomic"]),
        upstream_url=quote["upstream_url"],
        call_url=call_url,
        host=quote["host"],
        host_stats=quote["host_stats"],
        reason=quote["reason"],
    )


@app.post("/v1/warranty/call/{tier}")
async def call_warranty(tier: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    # The x402 middleware can only read the query string, so the upstream
    # target URL and method are passed there. Fall back to the JSON body for
    # direct/internal callers.
    query_url = request.query_params.get("url")
    query_method = request.query_params.get("method")
    if query_url:
        data["url"] = query_url
        if query_method:
            data["method"] = query_method

    req = WarrantyCallRequest(**data)
    upstream_url = req.url
    try:
        validate_url(upstream_url)
    except SSRFError as exc:
        return JSONResponse({"verdict": "error", "reason": str(exc)}, status_code=400)

    parsed = urlparse(upstream_url)
    upstream_host = parsed.netloc

    warranty_id = create_call(upstream_url, upstream_host, data)

    payload = getattr(request.state, "payment_payload", None)
    payer = ""
    paid_amount = 0
    if payload and payload.payload:
        raw = payload.payload
        if "authorization" in raw:
            payer = raw["authorization"].get("from", "")
        elif "permit2Authorization" in raw:
            payer = raw["permit2Authorization"].get("from", "")
        if payload.accepted:
            paid_amount = int(payload.accepted.amount)
    update_call(warranty_id, tier=tier, payer=payer)

    # Risk/eligibility check
    stats = host_stats(upstream_host)
    warranted, w_reason = is_warranted(stats)

    # Treasury floor check
    try:
        treasury_balance = get_treasury_balance()
    except Exception:
        treasury_balance = 0
    if treasury_balance < settings.treasury_floor_atomic:
        warranted = False
        w_reason = "treasury below floor"

    # Upstream price and tier limits (use the shared quote cache for consistency)
    try:
        quote = await _build_quote(req.url, req.method)
    except (SSRFError, ValueError) as exc:
        return JSONResponse({"verdict": "error", "reason": f"upstream probe failed: {exc}"}, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})

    upstream_price = quote["upstream_price_atomic"]

    # The upstream client is used for the actual upstream call below.
    client = _upstream_client()

    # Coverage limits: we already took payment, so a breach refunds (internal error)
    tier_caps = {"tier1": settings.tier1_max_atomic, "tier2": settings.tier2_max_atomic, "tier3": settings.tier3_max_atomic}
    breach_reason: str | None = None
    if upstream_price <= 0 or upstream_price > tier_caps.get(tier, 0):
        breach_reason = "internal_error: upstream price exceeds coverage"
    elif tier != "tier1" and stats["calls"] < settings.tier1_host_max_calls:
        breach_reason = "internal_error: tier requires more host history"
    elif req.max_price_usd:
        try:
            if upstream_price > int(float(req.max_price_usd) * 1_000_000):
                breach_reason = "internal_error: upstream price exceeds buyer max_price_usd"
        except ValueError:
            pass
    if breach_reason:
        update_call(warranty_id, verdict="refunded", reason=breach_reason)
        return await _do_refund(warranty_id, payer, paid_amount, upstream_host, upstream_price, breach_reason)
    if warranted:
        premium, _ = compute_premium(stats, upstream_price)
    else:
        premium = 0
    update_call(warranty_id, upstream_price_atomic=str(upstream_price), premium_atomic=str(premium))

    # Record buyer payment accounting. The cash premium actually retained is
    # whatever remains after the upstream cost is covered.
    retained_premium = max(0, paid_amount - upstream_price)
    add_accounting_entry(
        "buyer_payment",
        paid_amount,
        warranty_id=warranty_id,
        payer=payer,
        upstream_host=upstream_host,
        description="buyer paid warranty",
    )
    if retained_premium > 0:
        add_accounting_entry(
            "premium_retained",
            retained_premium,
            warranty_id=warranty_id,
            payer=payer,
            upstream_host=upstream_host,
            description="premium retained",
        )

    # If not warranted, pass-through only (no refunds)
    if not warranted:
        try:
            upstream_response = await client.call(req.url, req.method, req.headers, req.body)
        except Exception as exc:
            update_call(warranty_id, verdict="error", reason=f"upstream call failed: {exc}")
            return JSONResponse({"verdict": "error", "reason": f"upstream call failed: {exc}", "warranty_id": warranty_id, "upstream_host": upstream_host}, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})
        update_call(
            warranty_id,
            tier=tier,
            payer=payer,
            upstream_status=upstream_response.get("status"),
            verdict="pass",
            raw_response=upstream_response.get("body"),
            response_headers=json.dumps(upstream_response.get("headers", {})),
        )
        return JSONResponse({
            "verdict": "pass",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
            "data": upstream_response.get("body"),
            "note": f"inspect-only: {w_reason}",
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "pass"})

    # Warranted path: pay upstream, validate, refund if objective failure
    try:
        upstream_response = await client.call(req.url, req.method, req.headers, req.body)
    except UpstreamError as exc:
        # Upstream payment failed
        reason = "upstream_payment_failed"
        update_call(warranty_id, verdict="refunded", reason=reason, upstream_status=None)
        add_accounting_entry("upstream_payment_out", 0, warranty_id=warranty_id, payer=payer, upstream_host=upstream_host, description="upstream payment failed")
        return await _do_refund(warranty_id, payer, paid_amount, upstream_host, upstream_price, reason)
    except Exception as exc:
        reason = f"network_error: {exc}"
        update_call(warranty_id, verdict="refunded", reason=reason)
        return await _do_refund(warranty_id, payer, paid_amount, upstream_host, upstream_price, reason)

    add_accounting_entry(
        "upstream_payment_out",
        upstream_price,
        warranty_id=warranty_id,
        payer=payer,
        upstream_host=upstream_host,
        description="paid upstream",
    )

    is_good, reason = validate_response(upstream_response, req.expect)

    if is_good:
        update_call(
            warranty_id,
            tier=tier,
            payer=payer,
            upstream_status=upstream_response["status"],
            verdict="pass",
            raw_response=upstream_response["body"],
            response_headers=json.dumps(upstream_response.get("headers", {})),
        )
        return JSONResponse({
            "verdict": "pass",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
            "data": upstream_response["body"],
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "pass"})

    update_call(warranty_id, verdict="refunded", reason=reason, upstream_status=upstream_response.get("status"))
    return await _do_refund(warranty_id, payer, paid_amount, upstream_host, upstream_price, reason)


async def _do_refund(warranty_id: str, payer: str, amount: int, upstream_host: str, upstream_price: int, reason: str) -> JSONResponse:
    if not payer or amount <= 0:
        return JSONResponse({
            "verdict": "error",
            "reason": f"refund preconditions not met: payer={payer}, amount={amount}",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})

    # Idempotency: the refunds table is authoritative (UNIQUE warranty_id)
    existing_refund = get_refund_for_warranty(warranty_id)
    if existing_refund and existing_refund.get("refund_tx"):
        return JSONResponse({
            "verdict": "refunded",
            "reason": reason,
            "refund_tx": existing_refund["refund_tx"],
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "refunded"})
    existing = get_call(warranty_id)
    if existing and existing.get("refund_tx"):
        return JSONResponse({
            "verdict": "refunded",
            "reason": reason,
            "refund_tx": existing["refund_tx"],
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "refunded"})

    # Exposure checks
    if daily_host_payout(upstream_host) + amount > settings.per_host_daily_payout_cap_atomic:
        return JSONResponse({
            "verdict": "error",
            "reason": "per-host daily payout cap exceeded",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})

    if hourly_payer_refunds(payer) + amount > settings.per_payer_hourly_refund_cap_atomic:
        return JSONResponse({
            "verdict": "error",
            "reason": "per-payer hourly refund cap exceeded",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})

    try:
        refund_tx = send_refund(payer, str(amount))
    except Exception as exc:
        return JSONResponse({
            "verdict": "error",
            "reason": f"refund failed: {exc}",
            "warranty_id": warranty_id,
            "upstream_host": upstream_host,
        }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "error"})

    record_refund(warranty_id, payer, str(amount), refund_tx, upstream_host=upstream_host)
    update_call(warranty_id, refund_tx=refund_tx, verdict="refunded")
    add_accounting_entry(
        "refund_out",
        amount,
        warranty_id=warranty_id,
        payer=payer,
        upstream_host=upstream_host,
        tx_hash=refund_tx,
        description=reason,
    )
    return JSONResponse({
        "verdict": "refunded",
        "reason": reason,
        "refund_tx": refund_tx,
        "warranty_id": warranty_id,
        "upstream_host": upstream_host,
    }, headers={"X-Warranty-Id": warranty_id, "X-Warranty-Verdict": "refunded"})


@app.get("/v1/warranty/{warranty_id}")
async def receipt(warranty_id: str):
    call = get_call(warranty_id)
    if not call:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "warranty_id": call["id"],
        "created_at": call["created_at"],
        "upstream_host": call["upstream_host"],
        "upstream_url": call["upstream_url"],
        "tier": call["tier"],
        "verdict": call["verdict"],
        "reason": call["reason"],
        "payment_tx": call["payment_tx"],
        "refund_tx": call["refund_tx"],
    }


@app.get("/v1/stats")
async def get_stats():
    return stats()
