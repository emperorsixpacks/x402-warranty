"""M1 test buyer for Two Keys.

Starts a session, buys two clues, and (optionally) makes an accusation.
Run the Two Keys server first:

    ALLOW_LOCAL_UPSTREAM=true PYTHONPATH=server uv run uvicorn server.two_keys:app --port 8003

Then run this script:

    BUYER_PRIVATE_KEY=0x... uv run python server/test_two_keys_buyer.py
"""

import asyncio
import json
import os

import httpx
from eth_account import Account

from x402 import x402Client
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client


BASE_URL = "http://localhost:8003"


def _make_payload(session_id: str, evidence_id: int) -> dict:
    return {
        "url": f"{BASE_URL}/v1/game/{session_id}/evidence/{evidence_id}",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "expect": {"content_type": "application/json"},
        "max_price_usd": "0.30",
    }


async def _x402_post(client: x402Client, url: str, body: dict, max_price_usd: str = "0.30") -> tuple[int, dict]:
    async with x402HttpxClient(client) as http:
        response = await http.post(url, json=body)
        await response.aread()
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}
        return response.status_code, data


async def main():
    buyer = x402Client()
    private_key = os.environ.get("BUYER_PRIVATE_KEY")
    if not private_key:
        account = Account.create()
        private_key = account.key.hex()
        print(f"Generated test buyer private key: {private_key}")
        print(f"Buyer address: {account.address}")
    else:
        account = Account.from_key(private_key)
        print(f"Buyer address: {account.address}")
    register_exact_evm_client(buyer, EthAccountSigner(account))

    # 1. Start a free session
    start_resp = httpx.post(f"{BASE_URL}/v1/game/start", json={"team_name": "M1 Test Squad"})
    print("Start status:", start_resp.status_code)
    start = start_resp.json()
    print("Case file preview:", start["case_file"][:120], "...")
    print("Evidence menu:")
    for ev in start["evidence_menu"]:
        print(f"  {ev['id']}. {ev['name']} ({ev['price']}) - {ev['teaser']}")
    session_id = start["session_id"]
    print("Session ID:", session_id)

    # 2. Buy two clues
    clues_to_buy = [1, 5]  # badge log and shipping manifest
    for eid in clues_to_buy:
        print(f"\n--- Buying clue {eid} ---")
        body = _make_payload(session_id, eid)
        status, data = await _x402_post(buyer, body["url"], body)
        print(f"Status: {status}")
        print(json.dumps(data, indent=2))

    # 3. Accuse
    accuse_url = f"{BASE_URL}/v1/game/{session_id}/accuse"
    accuse_body = {
        "url": accuse_url,
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": {"suspect": "marcus-vega"},
        "expect": {"content_type": "application/json"},
        "max_price_usd": "0.10",
    }
    print("\n--- Making accusation ---")
    status, data = await _x402_post(buyer, accuse_url, accuse_body, max_price_usd="0.10")
    print(f"Status: {status}")
    print(json.dumps(data, indent=2))

    # 4. Receipt
    receipt = httpx.get(f"{BASE_URL}/v1/game/{session_id}/receipt")
    print("\n--- Receipt ---")
    print(json.dumps(receipt.json(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
