# Two Keys (in-chat x402 mystery game)

A redesign of the PayBox hackathon project: a detective game played entirely inside chat, with paid evidence sources and a receipt of every purchase.

## M1 status

- `POST /v1/game/start` (free) returns the case file, 5 suspects, evidence menu, and budget.
- Six paid evidence sources, priced $0.01-$0.25, each a separate x402 route.
- `POST /v1/game/{sid}/accuse` (paid, $0.10) one-shot final accusation.
- `GET /v1/game/{sid}/receipt` (free) lists purchases, tx hashes, duplicates, and score.
- `GET /v1/leaderboard` (free) is a stub for M1.
- `server/test_two_keys_buyer.py` demonstrates a funded wallet buying clues and accusing.

## Run the game server locally

```bash
cp .env.example .env
# Fill in at least TREASURY_ADDRESS and X402_NETWORK (default Base Sepolia).
# Source the env so server/config.py picks it up.
set -a; source .env; set +a

PYTHONPATH=server PORT=8003 uv run uvicorn server.two_keys:app --port 8003
```

## Play a round with a test buyer

1. Get a Base Sepolia wallet with USDC.
2. Run the buyer script:

```bash
export BUYER_PRIVATE_KEY=0x...
PYTHONPATH=server uv run python server/test_two_keys_buyer.py
```

The script starts a session, buys two clues, accuses Marcus Vega, and fetches the receipt.

## Endpoints

### Start a session

```bash
curl -X POST http://localhost:8003/v1/game/start \
  -H "Content-Type: application/json" \
  -d '{"team_name":"M1 Test Squad"}'
```

Response includes `session_id`, `case_file`, `suspects`, `evidence_menu`, `budget`, and `rules`.

### Buy evidence (paid, x402)

```bash
curl -X POST http://localhost:8003/v1/game/{session_id}/evidence/{1-6}
# Returns 402 Payment Required with x402 requirements header.
```

Prices:

| Evidence | Price |
|----------|-------|
| 1. Security Badge Scan Logs | $0.01 |
| 2. Keycard Access Records | $0.02 |
| 3. Cafeteria CCTV Still | $0.04 |
| 4. Email Server Metadata | $0.08 |
| 5. Quantum Core Shipping Manifest | $0.12 |
| 6. Night Guard Deposition | $0.25 |

### Accuse (paid, x402)

```bash
curl -X POST http://localhost:8003/v1/game/{session_id}/accuse \
  -H "Content-Type: application/json" \
  -d '{"suspect":"marcus-vega"}'
```

### Receipt

```bash
curl http://localhost:8003/v1/game/{session_id}/receipt
```

## Design notes

- Fixed case for M1: culprit is Marcus Vega. Evidence 1, 5, and 6 point to him.
- Full sweep costs $0.52, exceeding the $0.50 budget.
- Duplicate purchases are allowed but marked as duplicate in the receipt.
- Sessions expire after 30 minutes.
- Per-IP rate limiting on the free start endpoint (20 starts per minute).

## Next milestones

- M2: seeded cases, all clues, receipt ledger, duplicate-spend flags.
- M3: deploy, PayBox listing, real-chat playtest in Claude and ChatGPT.
- M4: leaderboard, demo script.
