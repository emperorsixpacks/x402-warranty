"""Integration tests for the v3 warranty flow (chain and upstream faked)."""

import asyncio
import json
from types import SimpleNamespace

from starlette.requests import Request

import main
from models import RefundReason


def _make_request(payload: dict, paid_amount: int, payer: str = "0xbuyer") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/warranty/call/tier1",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "state": {
            "payment_payload": SimpleNamespace(
                payload={"authorization": {"from": payer}},
                accepted=SimpleNamespace(amount=str(paid_amount)),
            )
        },
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    return Request(scope, receive)


def _call(env, payload: dict, paid_amount: int = 55000, tier: str = "tier1") -> dict:
    req = _make_request(payload, paid_amount)
    resp = asyncio.run(main.call_warranty(tier, req))
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return {"status": resp.status_code, "body": json.loads(resp.body), "headers": headers}


def _seed_warm_host(host: str, calls: int, refunds: int = 0):
    import ledger

    for i in range(calls):
        cid = ledger.create_call(f"http://{host}/x", host, {})
        ledger.update_call(cid, verdict="refunded" if i < refunds else "pass")


PAYLOAD = {"url": "http://warranted.test/data", "method": "GET", "headers": {}, "expect": {}}


def test_cold_host_is_inspect_only(env):
    """New host: no warranty, upstream failure yields error/pass but never a refund."""
    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, PAYLOAD)
    assert result["headers"]["x-warranty-verdict"] == "pass"
    assert "inspect-only" in result["body"]["note"]
    assert env.refunds == [], "warm-up hosts must never be refunded"


def test_warranted_host_refunds_on_500(env):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, PAYLOAD)
    assert result["status"] == 200
    assert result["body"]["verdict"] == "refunded"
    assert result["body"]["reason"] == RefundReason.NON_2XX.value
    assert result["body"]["refund_tx"].startswith("0xfakerefund")
    assert env.refunds == [("0xbuyer", "55000")]


def test_warranted_host_passes_good_response(env):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.response = {"status": 200, "headers": {"content-type": "application/json"}, "body": '{"ok":true}'}
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "pass"
    assert env.refunds == []


def test_upstream_payment_failure_refunds(env):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.raise_on_call = main.UpstreamError("facilitator rejected payment")
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "refunded"
    assert result["body"]["reason"] == "upstream_payment_failed"
    assert len(env.refunds) == 1


def test_invalid_json_refunds(env):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.response = {"status": 200, "headers": {"content-type": "application/json"}, "body": "{broken"}
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "refunded"
    assert result["body"]["reason"] == RefundReason.INVALID_JSON.value


def test_price_exceeding_tier_refunds(env):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.quote_price = 90_000  # tier1 cap is 50000
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "refunded"
    assert "exceeds coverage" in result["body"]["reason"]


def test_max_price_usd_refund(env):
    _seed_warm_host("warranted.test", calls=25)
    payload = dict(PAYLOAD, max_price_usd="0.005")  # upstream is $0.01
    result = _call(env, payload)
    assert result["body"]["verdict"] == "refunded"
    assert "max_price_usd" in result["body"]["reason"]


def test_limited_history_blocks_higher_tiers(env):
    """Hosts under TIER1_HOST_MAX_CALLS may only use tier1 coverage."""
    _seed_warm_host("young.test", calls=10)
    env.fake.quote_price = 100_000  # tier2 territory
    payload = dict(PAYLOAD, url="http://young.test/data")
    result = _call(env, payload, tier="tier2")
    assert result["body"]["verdict"] == "refunded"
    assert "host history" in result["body"]["reason"]


def test_refund_is_idempotent(env, monkeypatch):
    _seed_warm_host("warranted.test", calls=25)
    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    first = _call(env, PAYLOAD)
    assert first["body"]["verdict"] == "refunded"

    # Second attempt on the same warranty id must not send a second transfer
    import ledger

    wid = first["body"]["warranty_id"]
    ledger.update_call(wid, verdict="pass", reason=None, refund_tx=None)

    async def second():
        req = _make_request(PAYLOAD, 55000)
        return await main._do_refund(wid, "0xbuyer", 55000, "warranted.test", 10000, "non_2xx")

    resp = asyncio.run(second())
    body = json.loads(resp.body)
    assert body["refund_tx"] == first["body"]["refund_tx"]
    assert len(env.refunds) == 1, "duplicate refund must not transfer twice"


def test_host_daily_cap_blocks_refund(env):
    _seed_warm_host("warranted.test", calls=25)
    import ledger
    from config import settings

    # Pre-load the host's daily payout to just under the cap
    cid = ledger.create_call("http://warranted.test/old", "warranted.test", {})
    near_cap = settings.per_host_daily_payout_cap_atomic - 1000
    ledger.record_refund(cid, "0xother", str(near_cap), "0xoldtx", upstream_host="warranted.test")

    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "error"
    assert "per-host daily payout cap" in result["body"]["reason"]
    assert env.refunds == []


def test_payer_hourly_cap_blocks_refund(env):
    _seed_warm_host("warranted.test", calls=25)
    import ledger
    from config import settings

    cid = ledger.create_call("http://warranted.test/old", "warranted.test", {})
    near_cap = settings.per_payer_hourly_refund_cap_atomic - 1000
    ledger.record_refund(cid, "0xbuyer", str(near_cap), "0xoldtx", upstream_host="other.test")

    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, PAYLOAD)
    assert result["body"]["verdict"] == "error"
    assert "per-payer hourly refund cap" in result["body"]["reason"]
    assert env.refunds == []


def test_treasury_below_floor_disables_warranty(env, monkeypatch):
    _seed_warm_host("warranted.test", calls=25)
    monkeypatch.setattr(main, "get_treasury_balance", lambda: 100)  # below $2 floor
    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, PAYLOAD)
    assert "inspect-only" in result["body"]["note"]
    assert "treasury below floor" in result["body"]["note"]
    assert env.refunds == []


def test_high_refund_rate_host_not_warranted(env):
    _seed_warm_host("noisy.test", calls=30, refunds=12)
    payload = dict(PAYLOAD, url="http://noisy.test/data")
    env.fake.response = {"status": 500, "headers": {}, "body": "boom"}
    result = _call(env, payload)
    assert "inspect-only" in result["body"]["note"]
    assert env.refunds == []


def test_stats_endpoint_reports_ledger(env):
    _seed_warm_host("warranted.test", calls=25)
    result = asyncio.run(main.get_stats())
    hosts = {h["upstream_host"]: h for h in result["hosts"]}
    assert hosts["warranted.test"]["call_count"] == 25
