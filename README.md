# Market Data Engine

An independent market-data collection, analysis and swing-trading research service.

It collects daily and 4-hour candles for a fixed list of liquid US, UK and European
equities/ETFs and major cryptocurrencies, validates and stores them in PostgreSQL,
calculates technical indicators, and three times a day classifies each instrument as
READY, NEAR-READY, PREPARING or REJECTED for a roughly 1–10 day swing trade.

**This is a research tool only.** It does not place trades, manage positions or connect
to any broker or to any other bot. Nothing it produces is financial advice.

## What it does

| Stage | What happens |
|---|---|
| Collect | Fetches only candles it does not already hold (incremental), from the first provider that serves the instrument. |
| Validate | Rejects invalid OHLC, negative volume and duplicates; flags missing, stale and incomplete candles. Missing data is recorded as missing, never as zero. |
| Analyse | EMA 9/20/50/200, RSI 14, MACD, ATR 14, ADX 14, Bollinger Bands, relative and average volume, swing highs/lows, support/resistance, trend strength, relative strength vs a benchmark. |
| Detect | Tight bases under a pivot, breakout-and-retest, pullbacks to the 20-period average, early trends. It looks for setups that are forming, not the day's biggest gainers. |
| Score | A transparent score out of 100 with its five components and the reason for every point. |
| Track | Records what each candidate did 1, 3, 5, 10 and 20 trading days later. |

Signals use **completed candles only**. Every trigger, stop and target is an actual
high, low or swing point from the stored candles and is labelled with where it came from.

## Markets and data providers

| Provider | Used for | Key needed |
|---|---|---|
| Twelve Data | Primary source for equity and ETF candles (and crypto fallback) | `TWELVE_DATA_API_KEY` |
| Binance public market data | Crypto candles | none |
| CoinMarketCap | Crypto rank, market cap and 24h volume snapshots | `COINMARKETCAP_API_KEY` |
| Alpha Vantage | Cross-checks a small sample of US daily closes each weekday | `ALPHA_VANTAGE_API_KEY` |

Provider limits the engine respects:

- **Twelve Data** free (Basic) plan: 8 requests a minute, 800 a day, **US markets only**.
  UK and European symbols need a paid plan. When Twelve Data refuses a symbol the engine
  records it as unsupported, stops asking for that exchange for a week, and reports those
  instruments as REJECTED with "no data" — it never substitutes invented prices.
  The engine reads the real plan limits from Twelve Data at the start of each scan.
- **Alpha Vantage** free key: about 25 requests a day, so only 5 symbols are cross-checked per day.
- **Binance** refuses some regions on its main host; the engine uses Binance's market-data
  host first and falls back to Twelve Data for crypto if every Binance host is blocked.
- If a key is missing or a provider is down, that provider is skipped and the rest of the scan continues.

The instrument list is in `app/universe.py` (30 US, 14 UK, 10 European, 15 crypto).

## Classifications and scoring

| Component | Maximum |
|---|---|
| Pattern quality | 25 |
| Technical confirmation | 25 |
| Relative strength and market regime | 20 |
| Liquidity and data quality | 15 |
| Realistic reward/risk | 15 |

- **READY** needs all of: a valid trade plan, reward/risk of at least 2:1 after estimated
  costs, a verified score of 70 or more, no unverified inputs, PASS data quality,
  adequate liquidity, price within half an ATR of the trigger, and no "do not chase" warning.
- **NEAR-READY** is close but fails one of those; the reason is listed.
- **PREPARING** is an early setup worth watching.
- **REJECTED** includes downtrends, no structure, failed data quality and instruments with no data.
- **DO NOT CHASE** is a separate warning for extended moves (far above the 20-period
  average, very high RSI, or a multi-ATR run in five bars).

Inputs that cannot be verified are marked `UNVERIFIED` or `PENDING`, excluded from the
score's denominator, and the score is labelled **provisional**. A provisional score can never be READY.

Estimated round-trip costs used for reward/risk are assumptions, not measured spreads:
US 0.10%, UK shares 0.60% (includes stamp duty), Europe 0.25%, crypto 0.50%.
Change them with the `MDE_COST_PCT_*` variables.

## Schedule

All times are Europe/London and follow daylight saving automatically.

| Time | Scan |
|---|---|
| 07:30 | Morning scan |
| 14:15 | US pre-market scan |
| 18:00 | Review scan |

Saturdays and Sundays are crypto only. Each scheduled slot runs once, scans cannot overlap,
and there is no continuous polling. On startup the service runs one scan if none has
completed in the last 6 hours, so a new deployment collects data straight away.

## HTTP endpoints (read-only)

| Path | Purpose |
|---|---|
| `/health` | Liveness, database state, scheduler state |
| `/health/db` | Database connectivity (503 if down) |
| `/status` | Candle counts, last collection, last scan, provider health, next scheduled runs |
| `/scans/latest` | Latest completed scan with its non-rejected candidates |
| `/candidates?classification=READY` | Candidates from the latest scan, by classification |
| `/docs` | Interactive API documentation |

`POST /admin/scan` starts a manual scan and only exists if `ADMIN_TOKEN` is set
(send it in the `X-Admin-Token` header).

## Database tables

`exchanges`, `instruments`, `market_candles`, `market_snapshots`, `technical_indicators`,
`provider_requests`, `data_quality_checks`, `scan_runs`, `scan_candidates`,
`candidate_levels`, `candidate_outcomes`, `system_health`.

`market_candles` has a unique key on instrument, provider, timeframe and candle open time,
so a candle can never be stored twice. Schema changes are managed with Alembic (`migrations/`).

## Project layout

```
app/
  main.py            FastAPI app and startup
  config.py          settings from environment variables
  database.py        engine, sessions, DATABASE_URL handling, upserts
  models/            SQLAlchemy 2 models
  providers/         Twelve Data, Alpha Vantage, CoinMarketCap, Binance adapters
  collectors/        incremental candle collection, snapshots, cross-checks
  indicators/        technical indicators
  scanners/          setup detection, scoring, classification
  services/          scan orchestration, data quality, outcome tracking
  scheduler.py       Europe/London schedule
  health.py          HTTP endpoints
migrations/          Alembic migrations
tests/               pytest suite (mocked providers, temporary SQLite database)
Dockerfile           build, test and start
```

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL URL. `postgres://` and `postgresql://` are both accepted. |
| `TWELVE_DATA_API_KEY` | for equities | Twelve Data key |
| `ALPHA_VANTAGE_API_KEY` | optional | Alpha Vantage key |
| `COINMARKETCAP_API_KEY` | optional | CoinMarketCap key |
| `TZ`, `PYTHONUNBUFFERED` | optional | Container clock and unbuffered logs |
| `ADMIN_TOKEN` | optional | Enables `POST /admin/scan` |
| `MDE_DISABLE_SCHEDULER` | optional | `1` stops the scheduler and startup scan |

Secrets are read from the environment only. API keys, the database URL and its password
are stripped from every log line and stored error message.

## Deployment (Railway)

The `market-data-collector` service builds from the `Dockerfile` on every push to `main`.
The build runs the test suite and fails if any test fails. On start the container runs
`alembic upgrade head` and then `uvicorn app.main:app` on the port Railway supplies in `PORT`.
The health check path is `/health`.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in values, then export them
alembic upgrade head
uvicorn app.main:app --port 8000
```

## Tests

```bash
python -m pytest
```

Tests use mocked provider responses and a temporary SQLite database. They make no
network calls and use no API quota.

## Limitations

- No exchange holiday calendar: equity gap checks are approximate and 4-hour equity
  gaps are not checked.
- Outcomes use daily candles; if a single day's range spans both the stop and the
  target, the result is recorded as AMBIGUOUS, not guessed.
- Outcome statistics describe what happened after past candidates. They are not evidence
  that any setup is profitable, and the engine makes no such claim.
- UK prices from Twelve Data are in pence (GBX).
