"""The research universe: liquid US, UK and European equities/ETFs and major crypto.

Kept deliberately small so a full scan fits inside free-tier provider quotas.
Whether a provider actually serves a symbol is discovered at run time and
recorded on the instrument; nothing here asserts that it does.
"""
import logging
from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Exchange, Instrument
from app.timeutil import utcnow

log = logging.getLogger(__name__)

EXCHANGES: List[dict] = [
    dict(code="US", name="US equities (NYSE / NASDAQ / Arca)", region="US", country="United States",
         timezone="America/New_York", currency="USD", mic_code=None),
    dict(code="LSE", name="London Stock Exchange", region="UK", country="United Kingdom",
         timezone="Europe/London", currency="GBX", mic_code="XLON"),
    dict(code="XETR", name="Deutsche Boerse Xetra", region="EU", country="Germany",
         timezone="Europe/Berlin", currency="EUR", mic_code="XETR"),
    dict(code="XPAR", name="Euronext Paris", region="EU", country="France",
         timezone="Europe/Paris", currency="EUR", mic_code="XPAR"),
    dict(code="XAMS", name="Euronext Amsterdam", region="EU", country="Netherlands",
         timezone="Europe/Amsterdam", currency="EUR", mic_code="XAMS"),
    dict(code="CRYPTO", name="Crypto spot (24/7)", region="CRYPTO", country=None,
         timezone="UTC", currency="USD", mic_code=None),
]

_US_ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF"]
_US = [
    ("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ Trust"), ("IWM", "iShares Russell 2000 ETF"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF"), ("XLE", "Energy Select Sector SPDR"),
    ("XLF", "Financial Select Sector SPDR"), ("AAPL", "Apple"), ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"), ("AMZN", "Amazon"), ("GOOGL", "Alphabet"), ("META", "Meta Platforms"),
    ("TSLA", "Tesla"), ("AVGO", "Broadcom"), ("AMD", "Advanced Micro Devices"),
    ("JPM", "JPMorgan Chase"), ("XOM", "Exxon Mobil"), ("LLY", "Eli Lilly"),
    ("UNH", "UnitedHealth"), ("V", "Visa"), ("COST", "Costco"), ("NFLX", "Netflix"),
    ("CRM", "Salesforce"), ("ORCL", "Oracle"), ("PLTR", "Palantir"), ("CAT", "Caterpillar"),
    ("GE", "GE Aerospace"), ("BA", "Boeing"), ("WMT", "Walmart"), ("HD", "Home Depot"),
]
_UK = [
    ("ISF", "iShares Core FTSE 100 ETF", "etf"), ("SHEL", "Shell", "equity"),
    ("AZN", "AstraZeneca", "equity"), ("HSBA", "HSBC", "equity"), ("ULVR", "Unilever", "equity"),
    ("RIO", "Rio Tinto", "equity"), ("GSK", "GSK", "equity"), ("BARC", "Barclays", "equity"),
    ("LLOY", "Lloyds Banking Group", "equity"), ("TSCO", "Tesco", "equity"),
    ("DGE", "Diageo", "equity"), ("REL", "RELX", "equity"), ("LSEG", "London Stock Exchange Group", "equity"),
    ("GLEN", "Glencore", "equity"),
]
_EU = [
    ("EXS1", "iShares Core DAX ETF", "etf", "XETR"), ("SAP", "SAP", "equity", "XETR"),
    ("SIE", "Siemens", "equity", "XETR"), ("ALV", "Allianz", "equity", "XETR"),
    ("DTE", "Deutsche Telekom", "equity", "XETR"), ("MC", "LVMH", "equity", "XPAR"),
    ("TTE", "TotalEnergies", "equity", "XPAR"), ("AIR", "Airbus", "equity", "XPAR"),
    ("OR", "L'Oreal", "equity", "XPAR"), ("ASML", "ASML Holding", "equity", "XAMS"),
]
_CRYPTO = [
    ("BTC", "Bitcoin"), ("ETH", "Ethereum"), ("SOL", "Solana"), ("BNB", "BNB"), ("XRP", "XRP"),
    ("ADA", "Cardano"), ("DOGE", "Dogecoin"), ("LINK", "Chainlink"), ("AVAX", "Avalanche"),
    ("TRX", "TRON"), ("LTC", "Litecoin"), ("DOT", "Polkadot"), ("INJ", "Injective"),
    ("NEAR", "NEAR Protocol"), ("AAVE", "Aave"),
]


def build_universe() -> List[dict]:
    rows: List[dict] = []
    for symbol, name in _US:
        rows.append(dict(
            symbol=symbol, name=name, asset_class="etf" if symbol in _US_ETFS else "equity",
            region="US", exchange="US", currency="USD", td_symbol=symbol, td_mic_code=None,
            av_symbol=symbol, binance_symbol=None, cmc_symbol=None,
            benchmark_symbol="SPY", is_benchmark=symbol == "SPY",
        ))
    for symbol, name, asset_class in _UK:
        rows.append(dict(
            symbol=symbol, name=name, asset_class=asset_class, region="UK", exchange="LSE",
            currency="GBX", td_symbol=symbol, td_mic_code="XLON", av_symbol=None,
            binance_symbol=None, cmc_symbol=None, benchmark_symbol="ISF", is_benchmark=symbol == "ISF",
        ))
    for symbol, name, asset_class, mic in _EU:
        rows.append(dict(
            symbol=symbol, name=name, asset_class=asset_class, region="EU", exchange=mic,
            currency="EUR", td_symbol=symbol, td_mic_code=mic, av_symbol=None,
            binance_symbol=None, cmc_symbol=None, benchmark_symbol="EXS1", is_benchmark=symbol == "EXS1",
        ))
    for symbol, name in _CRYPTO:
        rows.append(dict(
            symbol=symbol, name=name, asset_class="crypto", region="CRYPTO", exchange="CRYPTO",
            currency="USD", td_symbol=f"{symbol}/USD", td_mic_code=None, av_symbol=None,
            binance_symbol=f"{symbol}USDT", cmc_symbol=symbol,
            benchmark_symbol="BTC", is_benchmark=symbol == "BTC",
        ))
    return rows


def seed_universe(session: Session) -> Dict[str, int]:
    """Insert missing exchanges/instruments and refresh provider mappings. Idempotent."""
    exchanges = {e.code: e for e in session.scalars(select(Exchange)).all()}
    added_exchanges = 0
    for spec in EXCHANGES:
        existing = exchanges.get(spec["code"])
        if existing is None:
            existing = Exchange(**spec)
            session.add(existing)
            exchanges[spec["code"]] = existing
            added_exchanges += 1
        else:
            for key, value in spec.items():
                setattr(existing, key, value)
    session.flush()

    instruments = {(i.symbol, i.region): i for i in session.scalars(select(Instrument)).all()}
    added = 0
    for spec in build_universe():
        spec = dict(spec)
        exchange = exchanges[spec.pop("exchange")]
        existing = instruments.get((spec["symbol"], spec["region"]))
        if existing is None:
            session.add(Instrument(exchange_id=exchange.id, active=True, **spec))
            added += 1
        else:
            for key, value in spec.items():
                setattr(existing, key, value)
            existing.exchange_id = exchange.id
            existing.updated_at = utcnow()
    session.flush()
    if added or added_exchanges:
        log.info("Universe seeded: %d new exchange(s), %d new instrument(s)", added_exchanges, added)
    return {"exchanges_added": added_exchanges, "instruments_added": added}
