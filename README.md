# Clawdbot X

**Utility:** $clawd — CA: `Hoi9Lo8s2PP7EM9mv9bZjQ3aSB7ijyS238sTqQjbpump`

Clawdbot X is a lightweight **crypto context + event engine** for LLM apps. It polls live signals, converts them into evidence-backed events, and exposes APIs for snapshots, diffs, prompt augmentation, and webhooks.

## Features
- Live signal polling (CoinGecko + Binance; optional Etherscan)
- Evidence-backed events (source URL + snippet + timestamp)
- Snapshots and diffs (`/snapshot`, `/diff`)
- Prompt augmentation for LLMs (`/augment`)
- Webhook registration for event alerts (`/webhook`)

## Quickstart (Local)
```bash
pip install -r requirements.txt
uvicorn clawdbot_x:app --host 0.0.0.0 --port 8080
```

Open:
- `http://localhost:8080/api/v1/status`
- `http://localhost:8080/api/v1/about`
- `http://localhost:8080/docs`

## API Endpoints
- `GET /api/v1/status` — health + collectors + utility info
- `GET /api/v1/about` — app metadata
- `GET /api/v1/events` — latest events (supports `window_s`, `max_events`, `assets=BTC,ETH`)
- `GET /api/v1/snapshot` — snapshot with `snapshot_id`
- `GET /api/v1/diff?from_snapshot_id=...&to_snapshot_id=...` — changes between snapshots
- `POST /api/v1/augment` — returns an LLM-ready prompt with a context block
- `POST /api/v1/webhook` — register a webhook for alerts

## Docker
```bash
docker build -t clawdbot-x .
docker run -p 8080:8080 clawdbot-x
```

## Deploy on Render (Docker)
1. Push this repo to GitHub
2. Render → **New** → **Web Service** → connect your repo
3. Environment: **Docker** (auto-detects `Dockerfile`)
4. Port: **8080**
5. Optional environment variables:
   - `CLAWDBOTX_POLL_SECONDS=15`
   - `ETHERSCAN_API_KEY=...` *(only if you enable the Etherscan collector in `clawdbot_x.py`)*
6. Deploy

### Test after deploy
- `GET https://<your-render-url>/api/v1/status`
- `GET https://<your-render-url>/api/v1/about`

## Configuration
- `CLAWDBOTX_POLL_SECONDS` — polling interval in seconds (default: 15)
- `ETHERSCAN_API_KEY` — required only if you enable the Etherscan collector
