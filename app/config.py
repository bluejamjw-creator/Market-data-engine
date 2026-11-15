"""Application settings, read from environment variables only.

Secrets are never given defaults and never logged. Use `get_settings()`.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: Optional[str]
    timezone: str
    twelve_data_api_key: Optional[str]
    alpha_vantage_api_key: Optional[str]
    coinmarketcap_api_key: Optional[str]
    admin_token: Optional[str]
    disable_scheduler: bool
    startup_scan_max_age_hours: float
    # Estimated round-trip trading costs (spread + fees), percent of price.
    # These are assumptions used for reward/risk, not measured spreads.
    cost_pct_us: float
    cost_pct_uk: float
    cost_pct_eu: float
    cost_pct_crypto: float
    http_timeout_seconds: float
    alpha_vantage_daily_checks: int

    def cost_pct(self, region: str, asset_class: str) -> float:
        if asset_class == "crypto":
            return self.cost_pct_crypto
        if region == "UK":
            # UK stamp duty applies to shares, not ETFs.
            return self.cost_pct_uk if asset_class == "equity" else self.cost_pct_us
        if region == "EU":
            return self.cost_pct_eu
        return self.cost_pct_us

    def secrets(self) -> list:
        """Values that must never appear in logs or stored error text."""
        values = [
            self.twelve_data_api_key,
            self.alpha_vantage_api_key,
            self.coinmarketcap_api_key,
            self.admin_token,
        ]
        if self.database_url:
            values.append(self.database_url)
            # also the password on its own
            try:
                rest = self.database_url.split("://", 1)[1]
                creds = rest.rsplit("@", 1)[0]
                if ":" in creds:
                    values.append(creds.split(":", 1)[1])
            except IndexError:
                pass
        return [v for v in values if v and len(v) >= 4]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        database_url=_env("DATABASE_URL"),
        timezone="Europe/London",  # fixed by design; TZ env only affects the OS clock
        twelve_data_api_key=_env("TWELVE_DATA_API_KEY"),
        alpha_vantage_api_key=_env("ALPHA_VANTAGE_API_KEY"),
        coinmarketcap_api_key=_env("COINMARKETCAP_API_KEY"),
        admin_token=_env("ADMIN_TOKEN"),
        disable_scheduler=_env_bool("MDE_DISABLE_SCHEDULER"),
        startup_scan_max_age_hours=_env_float("MDE_STARTUP_SCAN_MAX_AGE_HOURS", 6.0),
        cost_pct_us=_env_float("MDE_COST_PCT_US", 0.10),
        cost_pct_uk=_env_float("MDE_COST_PCT_UK", 0.60),
        cost_pct_eu=_env_float("MDE_COST_PCT_EU", 0.25),
        cost_pct_crypto=_env_float("MDE_COST_PCT_CRYPTO", 0.50),
        http_timeout_seconds=_env_float("MDE_HTTP_TIMEOUT_SECONDS", 20.0),
        alpha_vantage_daily_checks=int(_env_float("MDE_ALPHA_VANTAGE_DAILY_CHECKS", 5)),
    )
