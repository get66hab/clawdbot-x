"""
clawdbot_x.py

Clawdbot X — Crypto Context Engine (FastAPI)

Clawdbot X is a utility of:
$clawd CA: Hoi9Lo8s2PP7EM9mv9bZjQ3aSB7ijyS238sTqQjbpump

Features
- Collects live crypto signals on an interval (CoinGecko + Binance; optional Etherscan)
- Normalizes them into evidence-backed events
- Exposes: status, about, events, snapshot, diff, augment, webhook registration

Run (local dev):
  pip install -r requirements.txt
  uvicorn clawdbot_x:app --host 0.0.0.0 --port 8080

Docker:
  docker build -t clawdbot-x .
  docker run -p 8080:8080 clawdbot-x

Optional env:
  ETHERSCAN_API_KEY=...
  CLAWDBOTX_POLL_SECONDS=15
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl


# ----------------------------
# Config / Branding
# ----------------------------

APP_NAME = "Clawdbot X"
APP_VERSION = "0.3.0"
API_PREFIX = "/api/v1"

UTILITY_TOKEN_NAME = "$clawd"
UTILITY_TOKEN_CA = "Hoi9Lo8s2PP7EM9mv9bZjQ3aSB7ijyS238sTqQjbpump"
UTILITY_NOTE = f"{APP_NAME} is a utility of: {UTILITY_TOKEN_NAME} CA: {UTILITY_TOKEN_CA}"

POLL_SECONDS = int(os.getenv("CLAWDBOTX_POLL_SECONDS", "15"))
POLL_SECONDS = max(5, min(POLL_SECONDS, 300))  # 5s..5min


# ----------------------------
# Time + hashing helpers
# ----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ----------------------------
# API models
# ----------------------------

class Evidence(BaseModel):
    source: str = Field(..., description="Provider name, e.g., coingecko/binance/etherscan")
    url: HttpUrl = Field(..., description="Direct URL to supporting evidence")
    snippet: str = Field(..., description="Short excerpt or structured proof")
    observed_at_ms: int = Field(..., description="When this evidence was observed (ms epoch)")


class CryptoEvent(BaseModel):
    id: str = Field(..., description="Deterministic event id (hash)")
    snapshot_id: str = Field("", description="Snapshot id that included this event")
    observed_at_ms: int

    chain: str = Field(..., description="eth/sol/none/etc")
    asset: str = Field(..., description="BTC/ETH/SOL or protocol name")
    event_type: str = Field(..., description="price_move/funding_spike/whale_transfer/etc")

    title: str
    impact: str = Field(..., description="Why it matters in 1–2 lines")
    confidence: float = Field(..., ge=0.0, le=1.0)
    tags: List[str] = Field(default_factory=list)

    evidence: List[Evidence] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)


class Snapshot(BaseModel):
    snapshot_id: str
    generated_at_ms: int
    window_s: int
    events: List[CryptoEvent]


class AugmentRequest(BaseModel):
    prompt: str
    focus_assets: List[str] = Field(default_factory=list, description="e.g., ['BTC','ETH']")
    max_events: int = Field(default=25, ge=1, le=200)
    window_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)


class AugmentResponse(BaseModel):
    snapshot: Snapshot
    augmented_prompt: str


class DiffResponse(BaseModel):
    from_snapshot_id: str
    to_snapshot_id: str
    added: List[CryptoEvent]
    removed: List[str]
    unchanged: int


class WebhookRegister(BaseModel):
    url: HttpUrl
    secret: str = Field(..., min_length=8, description="Shared secret for signature verification")
    asset: str
    event_type: str
    min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)


# ----------------------------
# In-memory store (swap with Redis/Postgres later)
# ----------------------------

@dataclass
class InMemoryStore:
    events: List[CryptoEvent]
    snapshots: Dict[str, List[str]]
    webhooks: List[WebhookRegister]

    def __init__(self) -> None:
        self.events = []
        self.snapshots = {}
        self.webhooks = []

    def add_events(self, batch: List[CryptoEvent]) -> List[CryptoEvent]:
        """Append-only add. Returns newly added unique events."""
        existing = {e.id for e in self.events}
        added: List[CryptoEvent] = []
        for e in batch:
            if e.id in existing:
                continue
            self.events.append(e)
            added.append(e)
            existing.add(e.id)
        return added

    def build_snapshot(self, window_s: int, focus_assets: List[str], max_events: int) -> Snapshot:
        cutoff = now_ms() - window_s * 1000
        focus = {a.strip().upper() for a in focus_assets if a.strip()}

        ev = [e for e in self.events if e.observed_at_ms >= cutoff]
        if focus:
            ev = [e for e in ev if e.asset.upper() in focus]

        # Newest first; tie-break by confidence
        ev.sort(key=lambda x: (x.observed_at_ms, x.confidence), reverse=True)
        ev = ev[:max_events]

        gen = now_ms()
        snap_id = stable_hash({"generated_at_ms": gen, "event_ids": [e.id for e in ev], "window_s": window_s})

        # Assign snapshot_id on returned objects
        updated: List[CryptoEvent] = []
        for e in ev:
            if not e.snapshot_id:
                e.snapshot_id = snap_id
            updated.append(e)

        self.snapshots[snap_id] = [e.id for e in updated]
        return Snapshot(snapshot_id=snap_id, generated_at_ms=gen, window_s=window_s, events=updated)

    def diff(self, from_id: str, to_id: str) -> DiffResponse:
        if from_id not in self.snapshots or to_id not in self.snapshots:
            raise KeyError("unknown snapshot id")

        a = set(self.snapshots[from_id])
        b = set(self.snapshots[to_id])

        added_ids = list(b - a)
        removed_ids = list(a - b)
        unchanged = len(a & b)

        id_to_event = {e.id: e for e in self.events}
        added = [id_to_event[i] for i in added_ids if i in id_to_event]
        added.sort(key=lambda x: x.observed_at_ms, reverse=True)

        return DiffResponse(
            from_snapshot_id=from_id,
            to_snapshot_id=to_id,
            added=added,
            removed=removed_ids,
            unchanged=unchanged,
        )


STORE = InMemoryStore()


# ----------------------------
# Collectors (pluggable)
# ----------------------------

class Collector:
    name: str
    async def collect(self) -> List[CryptoEvent]:
        raise NotImplementedError


class CoinGeckoPriceCollector(Collector):
    """
    Demo price signal: uses CoinGecko 'simple/price' with 24h change.
    Emits price_move when abs(24h_change) exceeds threshold_pct.

    For true short windows (1m/5m/1h), use paid feeds or candles endpoints.
    """
    name = "coingecko"

    def __init__(self, assets: Dict[str, str], threshold_pct: float = 1.5) -> None:
        self.assets = assets  # symbol -> coingecko id
        self.threshold_pct = threshold_pct

    async def collect(self) -> List[CryptoEvent]:
        ids = ",".join(self.assets.values())
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"}

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()

        t = now_ms()
        out: List[CryptoEvent] = []

        for sym, cg_id in self.assets.items():
            row = data.get(cg_id, {})
            price = row.get("usd")
            chg24 = row.get("usd_24h_change")
            if price is None or chg24 is None:
                continue

            move = float(chg24)
            if abs(move) < self.threshold_pct:
                continue

            confidence = clamp(abs(move) / 12.0, 0.55, 0.90)
            title = f"{sym} moved {move:+.2f}% (24h)"
            impact = "Notable move; verify catalysts (flows, funding, news) before decisions."

            evidence_url = f"https://www.coingecko.com/en/coins/{cg_id}"
            event_id = stable_hash({
                "collector": self.name,
                "asset": sym,
                "type": "price_move",
                "minute_bucket": int(t / 60000),
                "move_round": round(move, 3),
            })

            out.append(CryptoEvent(
                id=event_id,
                snapshot_id="",
                observed_at_ms=t,
                chain="none",
                asset=sym,
                event_type="price_move",
                title=title,
                impact=impact,
                confidence=confidence,
                tags=["market", "price"],
                evidence=[Evidence(
                    source=self.name,
                    url=evidence_url,
                    snippet=f"usd={price}, usd_24h_change_pct={move:.4f}",
                    observed_at_ms=t,
                )],
                data={"usd": float(price), "usd_24h_change_pct": move},
            ))

        return out


class BinanceFundingCollector(Collector):
    """
    Funding signal via Binance futures premiumIndex endpoint.
    Emits funding_spike when abs(lastFundingRate) exceeds threshold.
    """
    name = "binance"

    def __init__(self, symbols: List[str], threshold: float = 0.0006) -> None:
        self.symbols = symbols  # e.g., BTCUSDT
        self.threshold = threshold

    async def collect(self) -> List[CryptoEvent]:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            rows = r.json()

        lookup = {row.get("symbol"): row for row in rows if isinstance(row, dict)}
        t = now_ms()
        out: List[CryptoEvent] = []

        for s in self.symbols:
            row = lookup.get(s)
            if not row:
                continue

            fr = float(row.get("lastFundingRate", 0.0))
            mp = float(row.get("markPrice", 0.0))
            if abs(fr) < self.threshold:
                continue

            asset = s.replace("USDT", "")
            confidence = clamp(abs(fr) / 0.002, 0.60, 0.92)
            title = f"{asset} funding {fr:+.6f}"
            impact = "Elevated funding can reflect crowded positioning and higher squeeze risk."

            event_id = stable_hash({
                "collector": self.name,
                "asset": asset,
                "type": "funding_spike",
                "minute_bucket": int(t / 60000),
                "rate_round": round(fr, 7),
            })

            out.append(CryptoEvent(
                id=event_id,
                snapshot_id="",
                observed_at_ms=t,
                chain="none",
                asset=asset,
                event_type="funding_spike",
                title=title,
                impact=impact,
                confidence=confidence,
                tags=["derivatives", "funding"],
                evidence=[Evidence(
                    source=self.name,
                    url="https://www.binance.com/en/futures",
                    snippet=f"symbol={s}, lastFundingRate={fr}, markPrice={mp}",
                    observed_at_ms=t,
                )],
                data={"symbol": s, "lastFundingRate": fr, "markPrice": mp},
            ))

        return out


class EtherscanAddressWatchCollector(Collector):
    """
    Optional: watches ETH addresses for large transfers using Etherscan.
    Requires ETHERSCAN_API_KEY.
    Emits whale_transfer when tx value exceeds threshold_eth.
    """
    name = "etherscan"

    def __init__(self, addresses: List[str], threshold_eth: float = 200.0) -> None:
        self.addresses = [a.lower() for a in addresses]
        self.threshold_eth = threshold_eth
        self.api_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
        self._seen: set[str] = set()

    async def collect(self) -> List[CryptoEvent]:
        if not self.api_key or not self.addresses:
            return []

        t = now_ms()
        out: List[CryptoEvent] = []

        async with httpx.AsyncClient(timeout=20) as client:
            for addr in self.addresses:
                params = {
                    "module": "account",
                    "action": "txlist",
                    "address": addr,
                    "sort": "desc",
                    "page": 1,
                    "offset": 10,
                    "apikey": self.api_key,
                }
                r = await client.get("https://api.etherscan.io/api", params=params)
                r.raise_for_status()
                j = r.json()

                txs = j.get("result", [])
                if not isinstance(txs, list):
                    continue

                for tx in txs:
                    h = tx.get("hash")
                    if not h or h in self._seen:
                        continue
                    self._seen.add(h)

                    try:
                        val_wei = float(tx.get("value", "0"))
                    except Exception:
                        continue

                    val_eth = val_wei / 1e18
                    if val_eth < self.threshold_eth:
                        continue

                    frm = (tx.get("from") or "").lower()
                    to_addr = (tx.get("to") or "").lower()
                    evidence_url = f"https://etherscan.io/tx/{h}"

                    event_id = stable_hash({"collector": self.name, "hash": h})

                    out.append(CryptoEvent(
                        id=event_id,
                        snapshot_id="",
                        observed_at_ms=t,
                        chain="eth",
                        asset="ETH",
                        event_type="whale_transfer",
                        title=f"Large ETH transfer ~{val_eth:.1f} ETH",
                        impact="Large transfers can precede exchange deposits/OTC activity; confirm destination label.",
                        confidence=0.70,
                        tags=["onchain", "transfer"],
                        evidence=[Evidence(
                            source=self.name,
                            url=evidence_url,
                            snippet=f"from={frm} to={to_addr} value_eth≈{val_eth:.4f}",
                            observed_at_ms=t,
                        )],
                        data={"hash": h, "from": frm, "to": to_addr, "value_eth": val_eth},
                    ))

        return out


# ----------------------------
# Webhook dispatch
# ----------------------------

def sign_payload(secret: str, payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"

async def dispatch_webhooks(new_events: List[CryptoEvent]) -> None:
    if not STORE.webhooks or not new_events:
        return

    async with httpx.AsyncClient(timeout=10) as client:
        for wh in STORE.webhooks:
            for ev in new_events:
                if ev.asset.upper() != wh.asset.upper():
                    continue
                if ev.event_type != wh.event_type:
                    continue
                if ev.confidence < wh.min_confidence:
                    continue

                body = {"event": ev.model_dump()}
                signature = sign_payload(wh.secret, body)

                try:
                    await client.post(
                        str(wh.url),
                        json=body,
                        headers={"X-ClawdbotX-Signature": signature},
                    )
                except Exception:
                    pass


# ----------------------------
# Context rendering
# ----------------------------

def render_context_block(snap: Snapshot) -> str:
    lines: List[str] = []
    lines.append(f"APP: {APP_NAME} v{APP_VERSION}")
    lines.append(f"UTILITY: {UTILITY_NOTE}")
    lines.append(f"SNAPSHOT_ID: {snap.snapshot_id}")
    lines.append(f"GENERATED_AT_MS: {snap.generated_at_ms}")
    lines.append(f"WINDOW_S: {snap.window_s}")
    lines.append("EVENTS:")

    for e in snap.events:
        lines.append(f"- [{e.event_type}] {e.asset} | {e.title}")
        lines.append(f"  observed_at_ms: {e.observed_at_ms} | chain: {e.chain} | confidence: {e.confidence:.2f}")
        lines.append(f"  impact: {e.impact}")
        for i, ev in enumerate(e.evidence[:3], start=1):
            snip = (ev.snippet or "")[:160]
            lines.append(f"  evidence_{i}: {ev.source} | {ev.url} | {snip}")

    return "\n".join(lines)


# ----------------------------
# App + router
# ----------------------------

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description=f"{UTILITY_NOTE}\n\nCrypto context + event engine for LLM apps.",
)

router = APIRouter(prefix=API_PREFIX, tags=["clawdbot-x"])

# Default collectors
COLLECTORS: List[Collector] = [
    CoinGeckoPriceCollector(
        assets={"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"},
        threshold_pct=1.5,
    ),
    BinanceFundingCollector(
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        threshold=0.0006,
    ),
    # Optional (requires ETHERSCAN_API_KEY):
    # EtherscanAddressWatchCollector(addresses=["0x..."], threshold_eth=200.0),
]


async def collector_loop() -> None:
    while True:
        try:
            batch: List[CryptoEvent] = []
            for c in COLLECTORS:
                try:
                    events = await c.collect()
                    batch.extend(events)
                except Exception:
                    continue

            new_events = STORE.add_events(batch)
            if new_events:
                await dispatch_webhooks(new_events)
        finally:
            await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(collector_loop())


# ----------------------------
# Endpoints
# ----------------------------

@router.get("/status")
async def status() -> Dict[str, Any]:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "ok": True,
        "server_time_ms": now_ms(),
        "poll_seconds": POLL_SECONDS,
        "utility": {"token": UTILITY_TOKEN_NAME, "ca": UTILITY_TOKEN_CA, "note": UTILITY_NOTE},
        "collectors": [getattr(c, "name", c.__class__.__name__) for c in COLLECTORS],
        "events_in_store": len(STORE.events),
        "snapshots": len(STORE.snapshots),
        "webhooks": len(STORE.webhooks),
    }


@router.get("/about")
async def about() -> Dict[str, Any]:
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "utility_of": UTILITY_TOKEN_NAME,
        "ca": UTILITY_TOKEN_CA,
        "note": UTILITY_NOTE,
        "api_prefix": API_PREFIX,
    }


@router.get("/events", response_model=List[CryptoEvent])
async def get_events(
    window_s: int = Query(3600, ge=60, le=7 * 24 * 3600),
    max_events: int = Query(50, ge=1, le=200),
    assets: Optional[str] = Query(None, description="Comma-separated symbols, e.g. BTC,ETH"),
) -> List[CryptoEvent]:
    focus: List[str] = []
    if assets:
        focus = [a.strip().upper() for a in assets.split(",") if a.strip()]
    snap = STORE.build_snapshot(window_s=window_s, focus_assets=focus, max_events=max_events)
    return snap.events


@router.get("/snapshot", response_model=Snapshot)
async def get_snapshot(
    window_s: int = Query(3600, ge=60, le=7 * 24 * 3600),
    max_events: int = Query(50, ge=1, le=200),
    assets: Optional[str] = Query(None, description="Comma-separated symbols, e.g. BTC,ETH"),
) -> Snapshot:
    focus: List[str] = []
    if assets:
        focus = [a.strip().upper() for a in assets.split(",") if a.strip()]
    return STORE.build_snapshot(window_s=window_s, focus_assets=focus, max_events=max_events)


@router.get("/diff", response_model=DiffResponse)
async def get_diff(from_snapshot_id: str, to_snapshot_id: str) -> DiffResponse:
    try:
        return STORE.diff(from_snapshot_id, to_snapshot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown snapshot id")


@router.post("/augment", response_model=AugmentResponse)
async def augment(req: AugmentRequest) -> AugmentResponse:
    snap = STORE.build_snapshot(window_s=req.window_s, focus_assets=req.focus_assets, max_events=req.max_events)
    context = render_context_block(snap)

    augmented = (
        "You are an assistant. Use ONLY the context block below for time-sensitive facts.\n"
        "If the context is insufficient, say exactly what is missing.\n\n"
        "=== CONTEXT BLOCK (AUTHORITATIVE, TIME-SENSITIVE) ===\n"
        f"{context}\n"
        "=== END CONTEXT BLOCK ===\n\n"
        "USER PROMPT:\n"
        f"{req.prompt}"
    )

    return AugmentResponse(snapshot=snap, augmented_prompt=augmented)


@router.post("/webhook")
async def register_webhook(req: WebhookRegister) -> Dict[str, Any]:
    STORE.webhooks.append(req)
    return {"ok": True, "registered": len(STORE.webhooks)}


app.include_router(router)

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return f"""
    <html>
      <head><title>{APP_NAME}</title></head>
      <body style="font-family: Arial, sans-serif; max-width: 720px; margin: 40px auto;">
        <h1>{APP_NAME}</h1>
        <p>{UTILITY_NOTE}</p>
        <ul>
          <li><a href="/api/v1/status">/api/v1/status</a></li>
          <li><a href="/api/v1/about">/api/v1/about</a></li>
          <li><a href="/api/v1/snapshot">/api/v1/snapshot</a></li>
          <li><a href="/docs">/docs</a></li>
        </ul>
      </body>
    </html>
    """

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
