# AutoTrade — BTCUSD Paper Futures

Rules-based multi-timeframe paper-trading engine for BTCUSD perpetual futures (Delta Exchange India), with:

- **FastAPI** engine + Delta WebSocket market feed  
- **React** neon dashboard (Vite) with live WebSockets  
- **PostgreSQL** persistence via Docker Compose  
- **JWT auth** — each user gets an isolated $250 paper account  

## Quick start (Docker)

```bash
cd crypto-paper-trade
cp .env.example .env   # optional — defaults work for local

# Build the React SPA once (Node 18+), then start the stack
cd frontend && npm install && npm run build && cd ..
docker compose up --build
```

> The `web` image copies `frontend/dist` into nginx. Rebuild the SPA whenever UI code changes.

| Service | URL |
|---------|-----|
| React UI | http://localhost:3000 |
| API | http://localhost:8000 |
| Postgres | localhost:5432 |

**Seed admin** (from `.env`):

- Email: `admin@autotrade.dev`
- Password: `admin12345`

Register additional users from the UI; each gets a fresh paper ledger.

## Local development

### Database

```bash
docker compose up -d db
export DATABASE_URL=postgresql+psycopg2://paper:paperpass@localhost:5432/paper_trading
```

### API

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` and `/ws` to `:8000`.

## Auth & WebSockets

- `POST /api/auth/register` · `POST /api/auth/login` · `GET /api/auth/me`
- `GET /api/dashboard` (Bearer JWT)
- `WS /ws/dashboard?token=…` — live P&L, positions, market, signal (~1s)
- `WS /ws/candles?token=…&interval=5m` — candle history + live bar updates for `lightweight-charts`

## Strategy defaults

- Capital: **$250** · Leverage: **100×** · Lot: **0.001 BTC**  
- Timezone: **Asia/Kolkata (IST)** for risk day, funding, journals  
- Auto paper fills only for users with `auto_trade=true` (seed admin)

## Environment

See [`.env.example`](.env.example) for `DATABASE_URL`, `JWT_SECRET`, `CORS_ORIGINS`, seed admin, and ports.
