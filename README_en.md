<p align="center"><a href="README.md">简体中文</a> | <b>English</b></p>

# Market Workbench

[![Version](https://img.shields.io/badge/version-1.8.2-1f6feb)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61dafb?logo=react&logoColor=white)](https://react.dev/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE)

A locally hosted research workbench for China A-shares, with Hong Kong and US coverage. Quotes, financials, fund flows, macro, sectors, timing and allocation, bonds, gold, oil, Fed probabilities, factor evaluation and public news in one interface. Data stays on your machine; you bring the AI connection; the project issues no stock picks or trading instructions.

v1.8.2 is about memory and safety: sector and thematic score builds run in short-lived worker processes, one at a time; cache hits no longer reparse snapshots from disk; date-filtered factor panels are read in chunks. Static-file serving now checks directory boundaries and scheduled score refreshes no longer report success after failing. Earlier releases and unreleased experimental work (bond candidate priors, gold environment V3, the factor lab) are in [CHANGELOG.md](CHANGELOG.md).

**Why this project**

- **Fully local** — FastAPI + React; accounts, portfolios and reports live in `~/.market-workbench/` on your machine. No cloud hosting, no telemetry.
- **Zero-key data** — Public endpoints: Eastmoney, Tencent, Sina, AkShare, FRED, EIA, IMF, Polymarket/Kalshi. Almost none require an API key. Core dependencies are lightweight; heavy data sources are optional (missing ones return 501 with install hints, other features keep working).
- **BYO AI** — Plug in your own OpenAI-compatible API or local CLI (Claude Code / Codex / Qwen / Gemini / DeepSeek). Keys are stored only in your browser.
- **Agent-ready** — Ships an MCP server with zero third-party dependencies, exposing 42 data tools to local agents (quotes, valuation, financials, flows, macro/liquidity composites, industry chains, bonds, gold/oil scores, timing allocation, factor backtests, and more).
- **Honest research boundaries** — Every indicator shows its source and freshness. No stock recommendations, price targets, or timing calls.

> Market Workbench is a derivative of [simonlin1212/Vibe-Research](https://github.com/simonlin1212/Vibe-Research), retaining its public-data and pluggable-AI foundations, and adding macro and liquidity views, bonds/gold/oil/Fed pages, timing & allocation, factor research, industry chains, fund workflows, account-scoped sync, and visible data-source freshness.

![Market overview](docs/screenshots/daily-review.png)

<details>
<summary>More screenshots</summary>

| Macro | Factor lab |
|---|---|
| ![Macro](docs/screenshots/macro.png) | ![Factor lab](docs/screenshots/factors.png) |

| Bonds | Timing & Allocation |
|---|---|
| ![Bonds](docs/screenshots/bonds.png) | ![Timing & Allocation](docs/screenshots/allocation.png) |

| Gold | Oil |
|---|---|
| ![Gold](docs/screenshots/gold.png) | ![Oil](docs/screenshots/oil.png) |

| News | Stock detail |
|---|---|
| ![News](docs/screenshots/intel.png) | ![Stock detail](docs/screenshots/stock-detail.png) |

</details>

## Feature overview

| Group | Module | What it covers |
|---|---|---|
| Market | Market overview | A-share indices and global markets, sentiment, turnover, sector flows; 1/3/5/10-year index percentiles and valuation percentiles; intraday charts and daily review |
| Market | Timing & Allocation | Macro × liquidity × market-confirm timing score (5 risk levels, risk-budget multiplier, cash floor) mapped to equity/bond/commodity/cash target weights and rebalancing notes; timing replay and All-A index charts share one date axis |
| Market | Macro | Macro indicators, module scores, and a composite score card (backtested weights) with source and freshness |
| Market | Liquidity | CN/US liquidity composite scores, liquidity signals, market-flow indicators |
| Market | Global expectations | Polymarket and Kalshi public probabilities with sources, refresh times, history and AI insight cards |
| Market | News | Public-news aggregation, news radar and announcement tracking |
| Portfolio | Watchlist / Portfolio | Grouped stock and ETF watchlists; equity and fund holdings, closed positions, return tracking |
| Portfolio | Securities | A-share, HK and US quotes; charts, valuation, financials, filings, reports, fund flows |
| Research | Factor lab | Price-volume factor evaluation (RankIC / quintile returns / turnover, Alphalens-style), formula-engine custom factors (25 price-volume + PIT financial fields, announcement-date aligned), saved research runs and exploratory backtests |
| Research | Industry research | Industry-chain depth (stage maps, profit distribution, bottleneck transmission), Shenwan industry and thematic-board scores |
| Research | Screening | Manager-First fund screening; natural-language stock screening (AI verifies each condition; no built-in candidate pool) |
| Research | Event analysis | Insider and shareholder increase lists with a transparent score: plan start/end dates, latest purchase date, purchased and planned amounts in separate columns |
| Research | Bonds | Same-input snapshots, an eight-dimension read, yield-curve comparison, standardised cash-flow and stress scenarios, short/medium/long maturity candidate priors with retrospective validation; experimental, not trading scores |
| Research | FedWatch | Next-meeting countdown, target range and spot anchor, a matrix comparing ZQ-futures-derived probabilities against Polymarket, 24h change, and year-end hike/cut counts and rate-level distribution |
| Research | Gold | Five-dimension, eight-indicator environment monitoring with fixed prior weights and contribution attribution; missing inputs pause the score; versioned records, a 20-day direction weak prior and a 20-day volatility-risk forecast; separate international/Shanghai prices and a labelled PAXG/USDT proxy |
| Research | Oil | 5-dimension, 8-indicator scoring (EIA/CFTC/GPR), crack-spread proxy and WTI off-hours intraday chart (Hyperliquid perpetuals, 7×24 including weekends) |
| Research | Research | Personal report archive, research notes and reflection review |
| System | Data-source health | Probes for upstream endpoints and datasets, with status and data timestamps (`/source-health`) |

## Architecture

```text
Browser ──► FastAPI (8900) ──┬─ /api: public market data (quotes/financials/macro/news)
                            │        + ~/.market-workbench/ (accounts SQLite, portfolios, reports)
                            │        + AI: user-configured API or local CLI
                            └─ serves frontend/dist statically (same process; for frontend
                               HMR development run vite separately on 5899, /api proxies to 8900)
```

- **Frontend**: React 19, TypeScript, Vite, Tailwind, ECharts
- **Backend**: FastAPI, SQLite (users/sessions), 60+ topic-split data modules under `backend/`
- **Background jobs**: score refreshes and cache warm-up register at ASGI startup; sector and theme score builds run in short-lived worker processes, one at a time
- **MCP server**: pure-stdlib JSON-RPC over stdio, sharing the same data-tool layer as the HTTP API (42 tools)

Module layout and data flow: [docs/architecture.md](docs/architecture.md).

## Quick start

Requirements: Python 3.10+, Node.js 20+.

```bash
git clone https://github.com/FLIER001/Market-Workbench.git
cd Market-Workbench

# Backend environment (the only required setup)
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..

# One command: builds frontend (incremental) + serves both from a single process
./run.sh
```

Open <http://127.0.0.1:8900>. On first launch (empty user database) you can register the primary account from the web page; registration then closes by default and accounts are managed via a CLI:

```bash
cd backend && .venv/bin/python add_user.py add <username>   # also list / passwd / remove
```

Verify with `curl -fsS http://127.0.0.1:8900/api/health`. Common issues (backend unreachable, missing data, data location after upgrades) are covered in [docs/getting-started.md](docs/getting-started.md).

> Both halves are served by one process (the backend serves `frontend/dist`); no separate vite server is needed. For frontend HMR development only, run `cd frontend && npm run dev` (port 5899, `/api` proxies to 8900).

## Connecting AI and agents

1. **Web UI** — In Settings, choose a local CLI or enter an OpenAI-compatible API endpoint, model and key. The configuration is stored only in your browser and submitted to your backend only when a model call is made.
2. **Agents** — Register the MCP server (same version and data layer as the HTTP API; no key needed locally):

```bash
cd backend
claude mcp add market-workbench -- "$(pwd)/.venv/bin/python" "$(pwd)/mcp_server.py"
```

See [backend/README.md](backend/README.md) for API groups and the full MCP tool list.

## Configuration and data

Local development needs no configuration: the frontend proxies `/api` to `http://127.0.0.1:8900` by default. For internet-facing deployments set the security variables:

| Variable | Default | Purpose |
|---|---|---|
| `MW_ALLOW_ORIGINS` | `*` | CORS allowlist; tighten to your frontend domain when public |
| `MW_API_KEY` | empty | When set, all `/api/*` (except health) require Bearer auth |
| `MW_ALLOW_REGISTRATION` | off | Set to 1 to reopen web registration |
| `MW_DATA_DIR` / `MW_REPORTS_DIR` | `~/.market-workbench/` | Where accounts/portfolios/reports are stored |
| `MW_DATA_PROXY` | off | Set to 1 only if the machine must use a system proxy to reach data sources |
| `MW_HOST` / `MW_PORT` | `127.0.0.1` / `8900` | Listen address and port used by `run.sh` |
| `MW_ENV_FILE` | `backend/.env.local` | Private config file loaded by `run.sh` |

The full list (including `MW_MEM_WATCHDOG`, `MW_EIA_API_KEY` and deep-analysis model overrides) is in [docs/configuration.md](docs/configuration.md); examples in [backend/.env.example](backend/.env.example).

## Development

```bash
# Backend tests (pytest)
cd backend && .venv/bin/python -m pytest tests -q

# Frontend tests and build (node --test / tsc + vite)
cd frontend && npm test && npm run build
```

`frontend/package.json` is the single source of truth for the version; the HTTP API, MCP server and UI all read from it. Releases in [CHANGELOG.md](CHANGELOG.md); plans in [ROADMAP.md](ROADMAP.md).

## Scope and disclaimer

Market Workbench organizes public data and information you enter yourself. Indicators, rankings and AI output are research references, not investment advice; the project does not recommend securities, forecast prices, time trades or promise returns. Data comes from public endpoints that may be delayed, missing or rate-limited; verify sources and decide independently.

Scores and forecasts in the bond, gold and factor modules are experimental, and some have not passed independent out-of-sample validation. Definitions and validation protocols are in [bonds V2](research/bonds_methodology_v2.md) and [gold V3](research/gold_methodology_v3.md); the factor lab API and research flow are in [factor-lab-api.md](docs/research/factor-lab-api.md).

## License

[MIT](LICENSE)
