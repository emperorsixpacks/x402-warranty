# Demo Video Script (under 60 seconds)

Scene 1: Show the PayBox agent interface.
- Agent: "Quote a warranty call to the flaky upstream API."
- Tool: `quote_warranty` with upstream URL.
- Response: `warranted: false, reason: "host still in warm-up period"` (cold host gate).

Scene 2: Warm-up is done (ledger pre-seeded with 20 calls), quote again.
- Response: `warranted: true, tier: tier1, premium_atomic: "750"` (dynamic per-host premium).
- PayBox shows the x402 payment proposal for upstream + premium.
- User approves.

Scene 3: Good call.
- Warranty server pays upstream, upstream returns good data.
- Response: `{"verdict": "pass", "data": {...}}`, header `x-warranty-verdict: pass`.

Scene 4: Upstream breaks objectively (`?fail=500`).
- Response: `{"verdict": "refunded", "reason": "non_2xx", "refund_tx": "0x..."}`
- Show block explorer with the USDC refund tx.

Scene 5: Punch the risk engine.
- `/v1/stats`: per-host refund rate crossed 25% -> `warranted: false`,
  "refund rate exceeds threshold" — the host is now uninsurable.

Closing line: "Priced like insurance, refunded by rules, capped so the pool never blows up."
