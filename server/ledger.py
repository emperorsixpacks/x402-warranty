"""SQLite ledger and risk engine for the warranty server."""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from config import settings


def init_db() -> None:
    db_path = _db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS warranty_calls (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                upstream_url TEXT NOT NULL,
                upstream_host TEXT NOT NULL,
                tier TEXT,
                upstream_price_atomic TEXT,
                premium_atomic TEXT,
                warranty_price_atomic TEXT,
                payer TEXT,
                payment_tx TEXT,
                upstream_status INTEGER,
                verdict TEXT,
                reason TEXT,
                refund_tx TEXT,
                raw_request TEXT,
                raw_response TEXT,
                response_headers TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refunds (
                id TEXT PRIMARY KEY,
                warranty_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payer TEXT NOT NULL,
                amount_atomic TEXT NOT NULL,
                refund_tx TEXT NOT NULL,
                upstream_host TEXT,
                UNIQUE(warranty_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounting (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                warranty_id TEXT,
                entry_type TEXT NOT NULL,
                amount_atomic TEXT NOT NULL,
                payer TEXT,
                upstream_host TEXT,
                tx_hash TEXT,
                description TEXT
            )
        """)
        conn.commit()
        # Migrate older DBs: refunds.upstream_host was added in v3
        try:
            conn.execute("ALTER TABLE refunds ADD COLUMN upstream_host TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def _db_path() -> str:
    return settings.database_url.replace("sqlite:///", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_call(upstream_url: str, upstream_host: str, raw_request: dict) -> str:
    call_id = str(uuid.uuid4())
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            "INSERT INTO warranty_calls (id, created_at, upstream_url, upstream_host, raw_request) VALUES (?, ?, ?, ?, ?)",
            (call_id, _now(), upstream_url, upstream_host, json.dumps(raw_request)),
        )
        conn.commit()
    return call_id


def update_call(call_id: str, **kwargs) -> None:
    with sqlite3.connect(_db_path()) as conn:
        for key, value in kwargs.items():
            conn.execute(f"UPDATE warranty_calls SET {key} = ? WHERE id = ?", (value, call_id))
        conn.commit()


def get_call(call_id: str) -> dict | None:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM warranty_calls WHERE id = ?", (call_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def add_accounting_entry(
    entry_type: str,
    amount_atomic: int,
    warranty_id: str | None = None,
    payer: str | None = None,
    upstream_host: str | None = None,
    tx_hash: str | None = None,
    description: str = "",
) -> None:
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            "INSERT INTO accounting (id, created_at, warranty_id, entry_type, amount_atomic, payer, upstream_host, tx_hash, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), _now(), warranty_id, entry_type, str(amount_atomic), payer, upstream_host, tx_hash, description),
        )
        conn.commit()


def record_refund(warranty_id: str, payer: str, amount_atomic: str, refund_tx: str, upstream_host: str = "") -> bool:
    """Record a refund. Returns True if newly recorded, False if already exists."""
    try:
        with sqlite3.connect(_db_path()) as conn:
            conn.execute(
                "INSERT INTO refunds (id, warranty_id, created_at, payer, amount_atomic, refund_tx, upstream_host) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), warranty_id, _now(), payer, amount_atomic, refund_tx, upstream_host),
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False


def get_refund_for_warranty(warranty_id: str) -> dict | None:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM refunds WHERE warranty_id = ?", (warranty_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def _rolling_window_start(days: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _today_start() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def host_stats(host: str, window_days: int = 30) -> dict:
    """Return per-host stats: calls, refunds, refund_rate, calls_today, refunds_today."""
    start = _rolling_window_start(window_days)
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT
                COUNT(*) as calls,
                SUM(CASE WHEN verdict = 'refunded' THEN 1 ELSE 0 END) as refunds
            FROM warranty_calls
            WHERE upstream_host = ? AND created_at >= ?
            """,
            (host, start),
        )
        row = cur.fetchone()
        calls = row["calls"] or 0
        refunds = row["refunds"] or 0
        rate = (refunds / calls) if calls > 0 else 0.0

        today = _today_start()
        cur = conn.execute(
            """
            SELECT COUNT(*) as calls_today,
                   SUM(CASE WHEN verdict = 'refunded' THEN 1 ELSE 0 END) as refunds_today
            FROM warranty_calls
            WHERE upstream_host = ? AND created_at >= ?
            """,
            (host, today),
        )
        today_row = cur.fetchone()
        warranted, reason = _warranted_from(calls, refunds)
        return {
            "host": host,
            "calls": calls,
            "refunds": refunds,
            "refund_rate": rate,
            "calls_today": today_row["calls_today"] or 0,
            "refunds_today": today_row["refunds_today"] or 0,
            "warranted": warranted,
            "reason": reason,
        }


def _warranted_from(calls: int, refunds: int) -> tuple[bool, str | None]:
    prior_calls = settings.prior_weight
    prior_refunds = int(prior_calls * settings.prior_refund_rate)
    total_calls = calls + prior_calls
    total_refunds = refunds + prior_refunds
    observed_rate = total_refunds / total_calls if total_calls > 0 else settings.prior_refund_rate
    if calls < settings.min_calls_for_warranty:
        return False, "host still in warm-up period"
    if observed_rate > settings.max_refund_rate_for_warranty:
        return False, "refund rate exceeds threshold"
    return True, None


def compute_premium(host_stats: dict, upstream_price_atomic: int) -> tuple[int, bool]:
    """Return (premium_atomic, warranted)."""
    calls = host_stats["calls"]
    refunds = host_stats["refunds"]
    # Bayesian posterior estimate
    prior_calls = settings.prior_weight
    prior_refunds = int(prior_calls * settings.prior_refund_rate)
    total_calls = calls + prior_calls
    total_refunds = refunds + prior_refunds
    observed_rate = total_refunds / total_calls if total_calls > 0 else settings.prior_refund_rate

    # Check warranty eligibility
    if calls < settings.min_calls_for_warranty:
        return 0, False
    if observed_rate > settings.max_refund_rate_for_warranty:
        return 0, False

    # Premium = (observed_refund_rate * 1.5 + service_fee) * upstream_price
    rate_bps = int(observed_rate * 1.5 * 10000) + settings.service_fee_bps
    floor_bps = settings.premium_floor_bps
    rate_bps = max(rate_bps, floor_bps)
    premium = (upstream_price_atomic * rate_bps) // 10000
    return premium, True


def is_warranted(host_stats: dict) -> tuple[bool, str | None]:
    return _warranted_from(host_stats["calls"], host_stats["refunds"])


def daily_host_payout(host: str) -> int:
    today = _today_start()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_atomic AS INTEGER)), 0) as total FROM refunds WHERE upstream_host = ? AND created_at >= ?",
            (host, today),
        )
        return cur.fetchone()["total"] or 0


def hourly_payer_refunds(payer: str) -> int:
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_atomic AS INTEGER)), 0) as total FROM refunds WHERE payer = ? AND created_at >= ?",
            (payer, hour_ago),
        )
        return cur.fetchone()["total"] or 0


def total_refunds() -> int:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT COALESCE(SUM(CAST(amount_atomic AS INTEGER)), 0) as total FROM refunds")
        return cur.fetchone()["total"] or 0


def total_premium_retained() -> int:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT COALESCE(SUM(CAST(amount_atomic AS INTEGER)), 0) as total FROM accounting WHERE entry_type = 'premium_retained'"
        )
        return cur.fetchone()["total"] or 0


def stats() -> dict:
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("""
            SELECT upstream_host,
                   COUNT(*) as call_count,
                   SUM(CASE WHEN verdict = 'refunded' THEN 1 ELSE 0 END) as refund_count
            FROM warranty_calls
            GROUP BY upstream_host
        """)
        hosts = [dict(row) for row in cur.fetchall()]
        return {
            "hosts": hosts,
            "total_refunds": total_refunds(),
            "total_premium_retained": total_premium_retained(),
            "loss_ratio": (total_refunds() / max(total_premium_retained(), 1)),
        }
