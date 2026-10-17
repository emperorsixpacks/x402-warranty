"""Shared fixtures for the warranty server test suite."""

from types import SimpleNamespace

import pytest

import main

FAKE_TREASURY_BALANCE = 10_000_000  # $10, above the floor


class FakeUpstreamClient:
    """Stands in for the real x402 buyer client."""

    response: dict = {"status": 200, "headers": {"content-type": "application/json"}, "body": '{"ok":true}'}
    quote_price: int = 10_000  # $0.01
    raise_on_call: Exception | None = None

    async def get_quote(self, url, method="GET", headers=None, body=None):
        return {
            "accepts": [
                {"scheme": "exact", "network": "eip155:84532", "amount": str(self.quote_price), "asset": "USDC"}
            ]
        }

    async def call(self, url, method="GET", headers=None, body=None):
        if self.raise_on_call:
            raise self.raise_on_call
        return dict(self.response)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    from config import settings

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'itest.db'}")
    main.init_db()
    # Fake hostnames do not resolve; SSRF rules are covered in test_unit.py
    monkeypatch.setattr(main, "validate_url", lambda url: None)

    fake = FakeUpstreamClient()
    monkeypatch.setattr(main, "_upstream_client", lambda: fake)
    monkeypatch.setattr(main, "get_treasury_balance", lambda: FAKE_TREASURY_BALANCE)
    main._quote_cache.clear()

    refunds: list[tuple[str, str]] = []

    def fake_send_refund(to: str, amount_atomic: str) -> str:
        refunds.append((to, amount_atomic))
        return f"0xfakerefund{len(refunds)}"

    monkeypatch.setattr(main, "send_refund", fake_send_refund)
    return SimpleNamespace(fake=fake, refunds=refunds)
