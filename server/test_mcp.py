"""Tests for the stateless Streamable-HTTP MCP endpoint (claude.ai connector)."""

import pytest
from fastapi.testclient import TestClient

import main
from config import settings


@pytest.fixture()
def client(env, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://warranty.example.com")
    return TestClient(main.app)


def _rpc(client, payload, mid=1):
    if "id" in payload:
        payload = {"jsonrpc": "2.0", "id": mid, **payload}
    res = client.post("/mcp", json=payload)
    return res


def _seed_warm_host(host: str, calls: int = 25):
    """Cold hosts are inspect-only (no warranty, no premium)."""
    import ledger

    for _ in range(calls):
        cid = ledger.create_call(f"http://{host}/x", host, {})
        ledger.update_call(cid, verdict="pass")


def _expected_warranty_price(host: str, upstream: int) -> str:
    import ledger

    premium, warranted = ledger.compute_premium(ledger.host_stats(host), upstream)
    assert warranted
    return str(upstream + premium)


def test_initialize_negotiates_client_version(client):
    res = _rpc(client, {"method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert res.status_code == 200
    body = res.json()
    assert body["result"]["protocolVersion"] == "2025-11-25"
    assert body["result"]["serverInfo"]["name"] == "x402-warranty"
    assert "tools" in body["result"]["capabilities"]


def test_initialize_falls_back_to_latest(client):
    res = _rpc(client, {"method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert res.json()["result"]["protocolVersion"] == "2025-11-25"


def test_initial_notification_accepted(client):
    res = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert res.status_code == 202
    assert res.content == b""


def test_tools_list(client):
    res = _rpc(client, {"method": "tools/list"})
    tools = res.json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["quote_warranty", "call_warranty"]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert "outputSchema" in tool


def test_get_is_405(client):
    assert client.get("/mcp").status_code == 405


def test_unknown_method(client):
    res = _rpc(client, {"method": "resources/list"})
    assert res.json()["error"]["code"] == -32601


def test_unknown_tool(client):
    res = _rpc(client, {"method": "tools/call", "params": {"name": "nope", "arguments": {}}})
    assert res.json()["error"]["code"] == -32602


def test_quote_tool(client, env):
    _seed_warm_host("api.example.com")
    res = _rpc(
        client,
        {
            "method": "tools/call",
            "params": {"name": "quote_warranty", "arguments": {"url": "https://api.example.com/data"}},
        },
    )
    result = res.json()["result"]
    assert "isError" not in result
    structured = result["structuredContent"]
    assert structured["tier"] == "tier1"
    assert structured["network"] == "eip155:84532"
    assert structured["warranted"] is True
    assert structured["warranty_price_atomic"] == _expected_warranty_price("api.example.com", 10_000)
    assert structured["call_url"].startswith("https://warranty.example.com/v1/warranty/call/tier1?")


def test_quote_tool_rejects_bad_method(client, env):
    res = _rpc(
        client,
        {
            "method": "tools/call",
            "params": {"name": "quote_warranty", "arguments": {"url": "https://api.example.com/x", "method": "DELETE"}},
        },
    )
    assert res.json()["error"]["code"] == -32602


def test_call_tool_tier_fallback_is_offline(client, env):
    res = _rpc(
        client,
        {
            "method": "tools/call",
            "params": {
                "name": "call_warranty",
                "arguments": {"url": "https://api.example.com/data", "tier": "tier1"},
            },
        },
    )
    result = res.json()["result"]
    proposal = result["structuredContent"]["io.paybox/transactionProposal"]
    assert proposal["kind"] == "x402"
    assert proposal["chain"] == "eip155:84532"
    assert proposal["payment"]["maxAmount"] == "75000"
    assert proposal["payment"]["asset"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    assert proposal["request"]["method"] == "POST"
    assert proposal["request"]["url"].startswith(
        "https://warranty.example.com/v1/warranty/call/tier1?url=https%3A%2F%2Fapi.example.com%2Fdata&method=GET"
    )
    # Exactly one '?' — a second query string would corrupt the price callback.
    assert proposal["request"]["url"].count("?") == 1
    assert proposal["request"]["body"]["url"] == "https://api.example.com/data"


def test_call_tool_from_quote(client, env):
    _seed_warm_host("api.example.com")
    res = _rpc(
        client,
        {
            "method": "tools/call",
            "params": {"name": "call_warranty", "arguments": {"url": "https://api.example.com/data"}},
        },
    )
    proposal = res.json()["result"]["structuredContent"]["io.paybox/transactionProposal"]
    assert proposal["payment"]["maxAmount"] == _expected_warranty_price("api.example.com", 10_000)
    assert proposal["request"]["url"].count("?") == 1
    assert "&method=GET" in proposal["request"]["url"]
