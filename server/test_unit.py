"""Unit tests for SSRF, validation, and the v3 risk engine."""

import pytest

from ssrf import SSRFError, validate_url
from validation import validate_response


def test_validate_url_blocks_private():
    with pytest.raises(SSRFError):
        validate_url("http://localhost/foo")
    with pytest.raises(SSRFError):
        validate_url("http://127.0.0.1/foo")
    with pytest.raises(SSRFError):
        validate_url("http://10.0.0.1/foo")


def test_validate_url_allows_http():
    validate_url("http://example.com/foo")
    validate_url("https://example.com/foo")


def test_validate_url_blocks_non_http():
    with pytest.raises(SSRFError):
        validate_url("ftp://example.com/foo")


def test_validate_response_good():
    resp = {"status": 200, "body": '{"ok":true}', "headers": {"content-type": "application/json"}}
    good, reason = validate_response(resp, None)  # type: ignore
    assert good
    assert reason is None


def test_validate_response_empty_body():
    resp = {"status": 200, "body": "", "headers": {}}
    good, reason = validate_response(resp, None)  # type: ignore
    assert not good
    assert reason.value == "empty_body"


def test_validate_response_non_2xx():
    resp = {"status": 500, "body": "error", "headers": {}}
    good, reason = validate_response(resp, None)  # type: ignore
    assert not good
    assert reason.value == "non_2xx"


def test_buyer_expect_never_triggers_refund():
    from models import ExpectConfig

    resp = {"status": 200, "body": '{"ok":true}', "headers": {"content-type": "application/json"}}
    expect = ExpectConfig(must_contain=["this string is absent"], min_bytes=10000)
    good, reason = validate_response(resp, expect)
    assert good, f"buyer expect must not refund in v3, got {reason}"


def test_invalid_json_content_type_refunds():
    resp = {"status": 200, "body": "not json at all", "headers": {"content-type": "application/json"}}
    good, reason = validate_response(resp, None)  # type: ignore
    assert not good
    assert reason.value == "invalid_json"


def test_non_json_content_type_may_be_invalid_json():
    resp = {"status": 200, "body": "plain text", "headers": {"content-type": "text/plain"}}
    good, reason = validate_response(resp, None)  # type: ignore
    assert good, f"text/plain bodies are allowed regardless of parseability: {reason}"


@pytest.fixture()
def tmp_ledger(monkeypatch, tmp_path):
    import ledger
    from config import settings

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'test.db'}")
    ledger.init_db()
    return ledger


def test_new_host_not_warranted(tmp_ledger):
    stats = tmp_ledger.host_stats("new.example.com")
    warranted, reason = tmp_ledger.is_warranted(stats)
    assert not warranted
    assert "warm-up" in reason


def test_premium_floor_on_warranted_host(tmp_ledger):
    for _ in range(25):
        cid = tmp_ledger.create_call("http://x.example.com/a", "x.example.com", {})
        tmp_ledger.update_call(cid, verdict="pass")
    stats = tmp_ledger.host_stats("x.example.com")
    warranted, reason = tmp_ledger.is_warranted(stats)
    assert warranted, reason
    premium, premium_warranted = tmp_ledger.compute_premium(stats, 100_000)  # $0.10 upstream
    assert premium_warranted
    assert premium >= 5_000, "premium must be at least the 5% floor"


def test_high_refund_rate_disables_warranty(tmp_ledger):
    for i in range(30):
        cid = tmp_ledger.create_call("http://bad.example.com/a", "bad.example.com", {})
        tmp_ledger.update_call(cid, verdict="refunded" if i < 10 else "pass")
    stats = tmp_ledger.host_stats("bad.example.com")
    warranted, reason = tmp_ledger.is_warranted(stats)
    assert not warranted
    assert "threshold" in reason


def test_refund_recorded_once(tmp_ledger):
    cid = tmp_ledger.create_call("http://y.example.com/a", "y.example.com", {})
    assert tmp_ledger.record_refund(cid, "0xpayer", "10000", "0xtx", upstream_host="y.example.com")
    assert not tmp_ledger.record_refund(cid, "0xpayer", "10000", "0xtx2", upstream_host="y.example.com")


def test_exposure_caps_accumulate(tmp_ledger):
    cid = tmp_ledger.create_call("http://z.example.com/a", "z.example.com", {})
    tmp_ledger.record_refund(cid, "0xpayer2", "500000", "0xtx", upstream_host="z.example.com")
    assert tmp_ledger.daily_host_payout("z.example.com") == 500_000
    assert tmp_ledger.hourly_payer_refunds("0xpayer2") == 500_000
    assert tmp_ledger.daily_host_payout("other.example.com") == 0
