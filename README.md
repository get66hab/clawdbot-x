# Clawdbot X

**Utility:** $clawd — CA: `Hoi9Lo8s2PP7EM9mv9bZjQ3aSB7ijyS238sTqQjbpump`

## Render deploy (Docker)
1) Put these files in a GitHub repo
2) Render → New → Web Service → connect your repo
3) Environment: **Docker** (Render auto-detects `Dockerfile`)
4) Set **Port = 8080**
5) Optional env vars:
   - `CLAWDBOTX_POLL_SECONDS=15`
   - `ETHERSCAN_API_KEY=...` (only if you enable the Etherscan collector in `clawdbot_x.py`)
6) Deploy

### Test
- `GET https://<your-render-url>/api/v1/status`
- `GET https://<your-render-url>/api/v1/about`

## Local run
```bash
pip install -r requirements.txt
uvicorn clawdbot_x:app --host 0.0.0.0 --port 8080
```

## Docker run
```bash
docker build -t clawdbot-x .
docker run -p 8080:8080 clawdbot-x
```
