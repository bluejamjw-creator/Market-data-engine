Market Data Engine

An independent market-data collection, analysis and swing-trading research platform.

Purpose

The Market Data Engine is designed to collect, validate, store and analyse financial market data to identify developing trading opportunities across global equities, ETFs and major cryptocurrencies.

The aim is to identify high-quality potential swing trades before they become obvious breakouts, rather than simply reporting assets that have already made large moves.

This is a research and analysis system only. It does not execute trades or connect to any brokerage account.

Markets covered

- US equities
- UK equities
- European equities
- ETFs
- Major cryptocurrencies

The system will use daily and 4-hour market data to identify developing setups suitable for approximately 1–10 day swing trades.

Data sources

The planned data providers are:

- Twelve Data – primary cross-market data.
- Alpha Vantage – secondary equity data and verification.
- CoinMarketCap – cryptocurrency market information.
- Binance public market data – historical cryptocurrency candles.

Provider availability, API limits and data quality will be checked before use.

Trading opportunity detection

The engine will analyse:

- Price trends and market structure.
- EMA 9, 20, 50 and 200.
- RSI, MACD, ATR and ADX.
- Bollinger Bands and volatility contraction.
- Relative volume and average volume.
- Support and resistance.
- Swing highs and lows.
- Breakouts, retests and early-stage momentum.
- Relative strength against the wider market.
- Potential reward-to-risk ratios.

It will distinguish between:

- READY – conditions support a potential trade.
- NEAR-READY – approaching a valid entry.
- PREPARING – an early developing setup worth monitoring.
- REJECTED – conditions do not meet the required criteria.

It will also flag opportunities that are already extended and should not be chased.

Scoring system

Potential opportunities will be assessed using a 100-point scoring model:

Category| Maximum
Chart pattern and structure| 25
Technical indicators| 25
Relative strength and market regime| 20
Liquidity and data quality| 15
Reward-to-risk| 15
Total| 100

A READY classification requires a realistic minimum reward-to-risk ratio of 2:1 after estimated costs.

Missing or unverified data must be clearly flagged. The system must never invent prices, indicators, volume, trade levels or performance statistics.

Scan schedule

The planned scan windows are:

- 07:30 UK time – morning market scan.
- 14:15 UK time – US pre-market scan.
- 18:00 UK time – market review.

During weekends, scans will focus on cryptocurrency markets only.

All scheduling will use the Europe/London timezone, including daylight-saving changes.

Historical performance tracking

The engine will record potential candidates and track their subsequent performance at:

- 1 trading day.
- 3 trading days.
- 5 trading days.
- 10 trading days.
- 20 trading days.

Where sufficient data is available, it will record maximum favourable and adverse movement, and whether price reached the planned target or invalidation level.

Historical results must be based on stored market observations, not assumptions or fabricated backtests.

Technology

The planned architecture uses:

- Python for data collection and analysis.
- PostgreSQL for persistent market data and scan history.
- Railway for hosting and scheduled execution.
- GitHub for version control and development.
- External market-data APIs for price and volume information.

Reliability and data integrity

The system must:

- Validate incoming market data.
- Detect missing, duplicated and incomplete candles.
- Handle API limits and temporary provider failures.
- Record scan history and provider errors.
- Avoid duplicate records and repeated processing.
- Keep API keys and database credentials secure.
- Never treat missing data as a confirmed trading signal.

Development principles

1. Data quality comes before trading signals.
2. No forced trades or artificial trade quotas.
3. Early developing opportunities are as important as confirmed breakouts.
4. Never chase an extended move.
5. Every proposed trade must have a clear trigger and invalidation level.
6. Never claim a result that cannot be verified.
7. Keep operating costs low and avoid unnecessary services.

Current development status

This project is under development.

The initial infrastructure includes a Railway project, PostgreSQL database and a GitHub-connected collector service. Application code, data collection, analysis and automated scans still need to be implemented and tested.

Long-term objective: Build a reliable, data-driven market research engine that consistently identifies and evaluates the strongest developing swing-trading opportunities without compromising data integrity.
