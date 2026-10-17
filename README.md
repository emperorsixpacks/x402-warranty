# x402 Warranty

A PayBox hackathon plugin: an x402 proxy that makes paid API calls money-back guaranteed — priced like micro-insurance.

## The v3 model (priced micro-insurance)

- **Refunds fire only on objective failures**: timeout, network error, non-2xx, empty body,
  invalid JSON when the response claims JSON, upstream payment failure, internal error,
  or a schema mismatch against the schema the upstream advertises in its 402 payload.
  The buyer's `expect` hints are accepted but never a refund event.
- **Per-host dynamic pricing**: premium = `max(5% floor, observed_refund_rate × 1.5 + 0.5% service fee)`
  of the upstream price, using a Bayesian prior (15% over the first 20 calls) for new hosts.
  The x402 route price is computed on the fly (upstream price + premium) and cached for
  five minutes so the quote and the actual charge always match.
- **Call-history gate**: hosts with fewer than 20 calls get no warranty (inspect-only pass-through).
  Hosts whose refund rate exceeds 25% are flagged and uninsurable.
- **Exposure caps**: per-host daily payout cap, per-payer hourly refund cap, tier coverage caps,
  and a treasury floor — below the floor, warranty is off until the treasury is topped up.
- **Idempotent refunds**: one refund per warranty id, enforced by a UNIQUE ledger row.
- **Accounting ledger**: buyer payments, premium retained, upstream payments, and refund
  payouts are all recorded; `/v1/stats` reports pool balance inputs and loss ratio.

## Architecture

- `server/`: Python FastAPI warranty server
  - `main.py`: quote, x402-protected `/v1/warranty/call/{tier}`, receipts, stats
  - `ledger.py`: SQLite ledger, per-host risk engine, exposure tracking, accounting
  - `validation.py`: objective refund rules (v3)
  - `upstream.py`: x402 buyer client for upstream payments
  - `refund.py`: USDC refund transfers from treasury (30s balance cache)
  - `ssrf.py`: SSRF protection
  - `demo.py`: demo upstream with objective failure modes (`fail=500|empty|badjson`)
  - `test_unit.py`, `test_integration.py`: 28 tests covering rules, caps, idempotency
- `plugin/`: TypeScript PayBox MCP plugin
  - `src/plugin.ts`: `call_warranty` and `quote_warranty` tools
  - `src/paybox.ts`: x402 proposal helpers

## Quick Start

### Server

```bash
cp .env.example .env
# Fill in your testnet keys
uv sync --extra dev

# Demo upstream (terminal 1)
set -a; source .env; set +a
PYTHONPATH=server uv run uvicorn server.demo:demo_app --port 8002

# Warranty server (terminal 2)
set -a; source .env; set +a
ALLOW_LOCAL_UPSTREAM=true PYTHONPATH=server uv run uvicorn server.main:app --port 8001
```

### Quote

```bash
curl "http://localhost:8001/v1/warranty/quote?url=http%3A%2F%2Flocalhost%3A8002%2Fdata"
# -> tier, warranted, premium_atomic, warranty_price_atomic, host_stats, reason
```

### Plugin

```bash
cd plugin
pnpm install --frozen-lockfile
pnpm build
cp dist/plugin.mjs ../paybox-plugin.mjs
# paybox-plugin.mjs is the artifact to upload
```

## Environment Variables

See `.env.example` for all required variables (pricing, exposure caps, risk thresholds).

## Tests

```bash
uv run pytest server/ -v
```

## Deployment

Build the Docker image and deploy to Fly.io, Railway, or Render. Set `PUBLIC_URL` to the deployed HTTPS URL.

## License

MIT
