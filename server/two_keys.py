"""Two Keys: in-chat multiplayer mystery game over x402.

M1 scope:
- POST /v1/game/start (free)
- POST /v1/game/{session_id}/evidence/{evidence_id} (paid, 6 sources)
- POST /v1/game/{session_id}/accuse (paid)
- GET  /v1/game/{session_id}/receipt (free)
- GET  /v1/leaderboard (free)

No warranty wrapper here: the game server accepts x402 payments directly.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from config import settings

# ---------------------------------------------------------------------------
# Game configuration
# ---------------------------------------------------------------------------

BUDGET = 50_000_000  # $0.50 in atomic USDC (6 decimals)
ACCUSE_PRICE = 10_000_000  # $0.10

EVIDENCE = [
    {
        "id": 1,
        "name": "Security Badge Scan Logs",
        "price_atomic": 10_000,      # $0.01
        "teaser": "After-hours badge swipes near the quantum vault.",
        "clue": (
            "Badge #447 was swiped at the loading dock at 2:03 AM. "
            "The badge is registered to Marcus Vega. Vega told investigators "
            "he left it in his locker at 8 PM and noticed it missing the next morning."
        ),
    },
    {
        "id": 2,
        "name": "Keycard Access Records",
        "price_atomic": 20_000,      # $0.02
        "teaser": "Lab floor entries on the night of the heist.",
        "clue": (
            "Dr. Aria Chen's keycard opened the quantum lab at 9:47 PM and again at 11:12 PM. "
            "She explained she was running an unscheduled calibration after a power fluctuation."
        ),
    },
    {
        "id": 3,
        "name": "Cafeteria CCTV Still",
        "price_atomic": 40_000,      # $0.04
        "teaser": "A late-evening exchange in the cafeteria.",
        "clue": (
            "Zara Okafor is seen handing a padded envelope to an unidentified courier at 6:15 PM. "
            "She claims it contained her resume and reference letters for a grad-school application."
        ),
    },
    {
        "id": 4,
        "name": "Email Server Metadata",
        "price_atomic": 80_000,      # $0.08
        "teaser": "Outgoing message headers from the night before.",
        "clue": (
            "Three encrypted archives were sent from Priya Kowalski's email account to a personal "
            "Gmail address between 11:00 PM and midnight. She says they were family photos."
        ),
    },
    {
        "id": 5,
        "name": "Quantum Core Shipping Manifest",
        "price_atomic": 120_000,     # $0.12
        "teaser": "Crate log for the loading bay.",
        "clue": (
            "A crate marked 'Q-Core Spare' was signed out at 2:31 AM by M. Vega. "
            "The destination is a private storage unit registered to his brother, Rafael Vega."
        ),
    },
    {
        "id": 6,
        "name": "Night Guard Deposition",
        "price_atomic": 250_000,     # $0.25
        "teaser": "Sworn statement from the only guard on duty.",
        "clue": (
            "Marcus Vega admitted he was paid $50,000 in cash to leave his post for 20 minutes "
            "and to lend his badge to a stranger. He would not identify the stranger."
        ),
    },
]

SUSPECTS = [
    {
        "id": "aria-chen",
        "name": "Dr. Aria Chen",
        "role": "Lead Quantum Physicist",
        "bio": "Brilliant, intense, and the only person with direct access to the core's calibration codes.",
    },
    {
        "id": "marcus-vega",
        "name": "Marcus Vega",
        "role": "Security Guard",
        "bio": "Overnight guard with a gambling debt and a brother in need of fast cash.",
    },
    {
        "id": "priya-kowalski",
        "name": "Priya Kowalski",
        "role": "Chief Financial Officer",
        "bio": "Controls the budget and recently approved a risky round of contractor hiring.",
    },
    {
        "id": "jim-holden",
        "name": "Dr. James 'Jim' Holden",
        "role": "Visiting Rival Scientist",
        "bio": "Published a scathing critique of Nebula Dynamics a month before the heist.",
    },
    {
        "id": "zara-okafor",
        "name": "Zara Okafor",
        "role": "Graduate Intern",
        "bio": "New hire with access to scheduling and a courier contact she will not name.",
    },
]

CULPRIT_ID = "marcus-vega"
CULPRIT_NAME = "Marcus Vega"
MOTIVE = "Vega was $80,000 in debt. A private broker paid him $50,000 to leave his post and hand over his badge, allowing the thief to enter the vault using a cloned badge and Vega's trusted presence on the logs."

CASE_INTRO = """Two Keys - Case File: The Nebula Dynamics Quantum Core Heist

Nebula Dynamics, a cutting-edge quantum computing firm, discovered that its flagship Quantum Core was stolen from the underground vault between 2:00 AM and 3:00 AM last Tuesday. The vault door was opened with a valid security badge, but the internal motion sensors were disabled for exactly 22 minutes. The company has five employees with access to the vault area, and a $0.50 budget to spend on evidence.

Your job: buy only the evidence you need, identify the culprit, and make the accusation. The case is solvable with smart spending; a full sweep of every source costs more than the budget.
"""

# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------

@dataclass
class Purchase:
    evidence_id: int
    price_atomic: int
    tx_hash: str | None
    duplicate: bool
    helped: bool | None = None


@dataclass
class Session:
    session_id: str
    created_at: float
    ip: str | None
    team_name: str | None
    purchases: list[Purchase] = field(default_factory=list)
    accused: bool = False
    won: bool | None = None
    accusation: str | None = None


_sessions: dict[str, Session] = {}
_last_accessed: dict[str, float] = {}
SESSION_TTL_SECONDS = 30 * 60


def _atomic_to_dollars(atomic: int) -> str:
    return f"${atomic / 1_000_000:.2f}"


def _money_str(atomic: int) -> str:
    return f"${atomic / 1_000_000:.2f}"


def _new_session(ip: str | None, team_name: str | None) -> Session:
    sid = str(uuid.uuid4())
    now = time.time()
    session = Session(session_id=sid, created_at=now, ip=ip, team_name=team_name)
    _sessions[sid] = session
    _last_accessed[sid] = now
    return session


def _get_session(sid: str) -> Session | None:
    now = time.time()
    _last_accessed[sid] = now
    session = _sessions.get(sid)
    if session is None:
        return None
    if now - session.created_at > SESSION_TTL_SECONDS:
        _sessions.pop(sid, None)
        _last_accessed.pop(sid, None)
        return None
    return session


def _cleanup_expired():
    now = time.time()
    expired = [sid for sid, t in _last_accessed.items() if now - t > SESSION_TTL_SECONDS]
    for sid in expired:
        _sessions.pop(sid, None)
        _last_accessed.pop(sid, None)


def _evidence_by_id(eid: int) -> dict | None:
    for ev in EVIDENCE:
        if ev["id"] == eid:
            return ev
    return None


# ---------------------------------------------------------------------------
# Receipt / leaderboard helpers
# ---------------------------------------------------------------------------

def _build_receipt(session: Session) -> dict:
    spent = sum(p.price_atomic for p in session.purchases)
    items = []
    for p in session.purchases:
        ev = _evidence_by_id(p.evidence_id)
        items.append({
            "evidence_id": p.evidence_id,
            "name": ev["name"] if ev else "Unknown",
            "price": _money_str(p.price_atomic),
            "price_atomic": p.price_atomic,
            "tx_hash": p.tx_hash,
            "duplicate": p.duplicate,
            "helped": p.helped,
        })
    return {
        "session_id": session.session_id,
        "team_name": session.team_name,
        "budget": _money_str(BUDGET),
        "spent": _money_str(spent),
        "remaining": _money_str(BUDGET - spent),
        "purchases": items,
        "accused": session.accused,
        "won": session.won,
        "accusation": session.accusation,
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Two Keys", description="In-chat x402 mystery game")

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
x402_server = x402ResourceServer(facilitator)
x402_server.register(settings.x402_network, ExactEvmServerScheme())


def _build_x402_routes() -> dict:
    routes: dict[str, RouteConfig] = {}
    for ev in EVIDENCE:
        eid = ev["id"]
        routes[f"POST /v1/game/:session_id/evidence/{eid}"] = RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    price=_money_str(ev["price_atomic"]),
                    network=settings.x402_network,
                    pay_to=settings.treasury_address,
                )
            ],
            description=f"Buy clue for evidence {eid}: {ev['name']}",
            mime_type="application/json",
        )
    routes["POST /v1/game/:session_id/accuse"] = RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=_money_str(ACCUSE_PRICE),
                network=settings.x402_network,
                pay_to=settings.treasury_address,
            )
        ],
        description="Make a final accusation",
        mime_type="application/json",
    )
    return routes


app.add_middleware(PaymentMiddlewareASGI, routes=_build_x402_routes(), server=x402_server)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "game": "two-keys"}


@app.post("/v1/game/start")
async def start_game(request: Request):
    """Start a new Two Keys session. Free."""
    _cleanup_expired()
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    team_name = body.get("team_name") if isinstance(body, dict) else None
    if not isinstance(team_name, str) or not team_name:
        team_name = "Team PayBox"
    ip = request.client.host if request.client else None
    session = _new_session(ip, team_name)

    evidence_menu = [
        {
            "id": ev["id"],
            "name": ev["name"],
            "price": _money_str(ev["price_atomic"]),
            "price_atomic": ev["price_atomic"],
            "teaser": ev["teaser"],
        }
        for ev in EVIDENCE
    ]
    rules = (
        "You are playing Two Keys, a cooperative in-chat mystery. "
        "The agent may propose evidence to buy; the human approves each purchase. "
        "Goal: identify the culprit, spend as little of the budget as possible, "
        "and make the final accusation. Set PayBox to ask before purchases."
    )
    return {
        "session_id": session.session_id,
        "case_file": CASE_INTRO,
        "suspects": SUSPECTS,
        "evidence_menu": evidence_menu,
        "budget": _money_str(BUDGET),
        "budget_atomic": BUDGET,
        "rules": rules,
    }


@app.post("/v1/game/{session_id}/evidence/{evidence_id}")
async def buy_evidence(session_id: str, evidence_id: int, request: Request):
    session = _get_session(session_id)
    if not session:
        return JSONResponse({"error": "session expired or not found"}, status_code=404)

    ev = _evidence_by_id(evidence_id)
    if not ev:
        return JSONResponse({"error": "invalid evidence id"}, status_code=400)

    already = [p for p in session.purchases if p.evidence_id == evidence_id]
    duplicate = bool(already)

    # Extract tx hash from payment payload for the receipt
    tx_hash = None
    payload = getattr(request.state, "payment_payload", None)
    if payload and payload.payload:
        raw = payload.payload
        if "authorization" in raw:
            tx_hash = raw["authorization"].get("txHash") or raw["authorization"].get("transactionHash")
        elif "permit2Authorization" in raw:
            tx_hash = raw["permit2Authorization"].get("txHash") or raw["permit2Authorization"].get("transactionHash")

    # Mark whether this clue pointed to the culprit
    helped = evidence_id in (1, 5, 6)
    session.purchases.append(Purchase(
        evidence_id=evidence_id,
        price_atomic=ev["price_atomic"],
        tx_hash=tx_hash,
        duplicate=duplicate,
        helped=helped,
    ))

    return {
        "session_id": session_id,
        "evidence_id": evidence_id,
        "name": ev["name"],
        "clue": ev["clue"],
        "duplicate": duplicate,
        "note": "This clue will appear in your receipt." if not duplicate else "Duplicate purchase; no new information.",
    }


@app.post("/v1/game/{session_id}/accuse")
async def accuse(session_id: str, request: Request):
    session = _get_session(session_id)
    if not session:
        return JSONResponse({"error": "session expired or not found"}, status_code=404)
    if session.accused:
        return JSONResponse({"error": "accusation already made"}, status_code=400)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    suspect_id = (body.get("suspect") if isinstance(body, dict) else None) or ""
    suspect_name = ""
    for s in SUSPECTS:
        if s["id"] == suspect_id:
            suspect_name = s["name"]
            break

    won = suspect_id == CULPRIT_ID
    session.accused = True
    session.won = won
    session.accusation = suspect_name or suspect_id

    tx_hash = None
    payload = getattr(request.state, "payment_payload", None)
    if payload and payload.payload:
        raw = payload.payload
        if "authorization" in raw:
            tx_hash = raw["authorization"].get("txHash") or raw["authorization"].get("transactionHash")
        elif "permit2Authorization" in raw:
            tx_hash = raw["permit2Authorization"].get("txHash") or raw["permit2Authorization"].get("transactionHash")

    spent = sum(p.price_atomic for p in session.purchases) + ACCUSE_PRICE
    walkthrough = (
        "Walkthrough: Marcus Vega was the inside man. The badge scan (evidence 1) showed his "
        "badge entered the loading dock at 2:03 AM. The shipping manifest (evidence 5) proved "
        "he signed out a crate containing the Quantum Core. His deposition (evidence 6) confessed "
        "he was paid to abandon his post. Dr. Chen's late lab work, Priya's emails, and Zara's "
        "courier exchange were red herrings or coincidences."
    )
    return {
        "session_id": session_id,
        "result": "win" if won else "lose",
        "won": won,
        "correct_suspect": CULPRIT_NAME,
        "your_accusation": suspect_name or suspect_id,
        "spent": _money_str(spent),
        "walkthrough": walkthrough,
        "accusation_tx_hash": tx_hash,
    }


@app.get("/v1/game/{session_id}/receipt")
async def receipt(session_id: str):
    session = _get_session(session_id)
    if not session:
        return JSONResponse({"error": "session expired or not found"}, status_code=404)
    return _build_receipt(session)


@app.get("/v1/leaderboard")
async def leaderboard():
    """Stub leaderboard; populated when sessions end."""
    return {"leaderboard": [], "note": "Leaderboard is a stub in M1."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
