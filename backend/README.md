# Backend

FastAPI service for Market Workbench. It provides market data, user-local research data, AI request routing and an MCP server. The service is intended for local or self-hosted use.

## Run locally

The recommended entrypoint is the repo-root `./run.sh` (builds the frontend if stale, then serves both the API and `frontend/dist` from a single process):

```bash
./run.sh
curl -fsS http://127.0.0.1:8900/api/health
```

Backend-only (no frontend static hosting) works as before:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8900
```

The frontend development server proxies `/api` to this address by default.

## HTTP API

OpenAPI is available at <http://127.0.0.1:8900/docs> while the service is running. The main route groups are:

| Group | Routes |
|---|---|
| Health and accounts | `/api/health`, `/api/auth/*` |
| AI | `/api/chat`, `/api/reflect`, `/api/deep-analysis` |
| Portfolio and reports | `/api/portfolio*`, `/api/fund-portfolio*`, `/api/myreports*` |
| Funds | `/api/funds/search`, `/api/funds/quote`, `/api/funds/nav/{code}`, `/api/funds/metrics/{code}`, `/api/funds/profile/{code}`, `/api/funds/screen` |
| Market and research | `/api/market/*`, `/api/radar*`, `/api/sector-scores*`, `/api/plate-scores*`, `/api/gold/score` |
| Securities | `/api/search`, `/api/quote`, `/api/kline*`, `/api/valuation*`, `/api/financials`, `/api/reports`, `/api/announcements`, `/api/news` |
| Capital and events | `/api/margin`, `/api/fund-flow`, `/api/holders`, `/api/dividend`, `/api/dragon-tiger`, `/api/lockup`, `/api/blocks` |
| Global markets | `/api/global/indices`, `/api/global/minute`, `/api/global/stock`, `/api/global/hk/cashflow` |

Several market sources are fetched on demand and may be delayed, unavailable or rate-limited. API responses include the data that is currently available; callers should not turn them into trading recommendations.

## MCP server

From `backend/`, register the server with a local client such as Claude Code:

```bash
claude mcp add market-workbench -- \
  "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"
```

The MCP server reads the same version and uses the same data-tool layer as the HTTP API. It does not need an API key for a local installation.

## Configuration

Copy [`.env.example`](.env.example) if you need configuration. Local development works with the defaults. For an internet-facing deployment, set both `MW_ALLOW_ORIGINS` and a strong `MW_API_KEY`.

| Variable | Purpose |
|---|---|
| `MW_ALLOW_ORIGINS` | Comma-separated CORS allowlist; `*` is the local-development default |
| `MW_API_KEY` | Enables bearer-token protection for `/api/*` except health |
| `MW_DATA_DIR` | Base directory for local portfolio, reports and user data |
| `MW_REPORTS_DIR` | Overrides the report archive directory only |
| `MW_DATA_PROXY` | Set to `1` only when data sources must use the system proxy |
| `IWENCAI_API_KEY` | Optional key for iWenCai report search |

See the root [configuration guide](../docs/configuration.md) for storage and deployment details.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

## Data and security notes

- Account data is stored in SQLite at `~/.market-workbench/users.db` by default. Passwords are salted and hashed; browser sessions use random tokens.
- Portfolio and report files are stored under `~/.market-workbench/` unless redirected by environment variables.
- The service is not a multi-tenant hosted platform. Do not expose it publicly without configuring CORS, an API key, TLS and your own access controls.
