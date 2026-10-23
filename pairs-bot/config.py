"""Configuration: Alpaca paper credentials, the pair, signal thresholds and risk limits.

Values come from environment variables (or a local ``.env`` file if
python-dotenv is installed). Everything is collected into one frozen
``Settings`` object so no module can change a limit at runtime.

Run modes
---------
``BOT_MODE=alpaca``  live market data + Alpaca *paper* account (keys required)
``BOT_MODE=sim``     fully offline: synthetic cointegrated prices + simulated broker.
                     Used for tests and for trying the dashboard without keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load .env next to this file
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # pragma: no cover
    pass


class ConfigError(ValueError):
    """A setting is missing or outside its safe range."""


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _f(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass(frozen=True, slots=True)
class Settings:
    # --- mode and credentials ------------------------------------------------
    mode: str = "sim"                       # "alpaca" or "sim"
    api_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    # Hard-wired: this project only ever talks to the PAPER endpoint.
    paper: bool = True
    data_feed: str = "iex"                  # "iex" is free; "sip" needs a paid data plan

    # --- the pair --------------------------------------------------------------
    # Spread = log(A) - beta * log(B) - alpha. Classic, liquid, economically linked pair.
    symbol_a: str = "KO"
    symbol_b: str = "PEP"

    # --- data / timing -----------------------------------------------------------
    timeframe_minutes: int = 1              # bar size
    hedge_lookback: int = 390               # bars used to estimate alpha/beta (≈1 trading day of 1-min bars)
    z_lookback: int = 60                    # bars for the rolling mean/std of the spread
    poll_seconds: float = 60.0              # one decision per new bar
    max_bar_age_seconds: float = 180.0      # refuse to trade on stale data

    # --- signal thresholds (in standard deviations of the spread) ---------------
    entry_z: float = 2.0                    # open when |z| >= 2.0
    exit_z: float = 0.5                     # close when |z| <= 0.5 (spread has mean-reverted)
    stop_z: float = 3.5                     # close at a loss when |z| >= 3.5 (relationship may be breaking)
    max_holding_bars: int = 390             # time stop: close if not reverted within this many bars
    min_coint_pvalue: float = 0.05          # only open if Engle-Granger p-value < this

    # --- sizing and risk limits --------------------------------------------------
    leg_notional: float = 2_000.0           # $ exposure on the A leg; B leg is beta-weighted
    max_gross_exposure: float = 10_000.0    # |A| + |B| market value cap
    max_daily_loss_usd: float = 300.0       # vs. previous close equity
    max_daily_loss_pct: float = 0.02        # 2% of previous close equity; the tighter of the two wins
    max_consecutive_errors: int = 5         # API/data errors in a row before halting

    # --- storage -----------------------------------------------------------------
    db_path: str = "pairs_bot.db"

    def __post_init__(self) -> None:
        if self.mode not in ("alpaca", "sim"):
            raise ConfigError("BOT_MODE must be 'alpaca' or 'sim'")
        if self.mode == "alpaca" and not (self.api_key and self.secret_key):
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required in alpaca mode")
        if self.paper is not True:
            raise ConfigError("this project only supports Alpaca paper trading")
        if self.symbol_a == self.symbol_b:
            raise ConfigError("the two symbols must differ")
        if not (0 < self.exit_z < self.entry_z < self.stop_z):
            raise ConfigError("need 0 < exit_z < entry_z < stop_z")
        if self.z_lookback < 20 or self.hedge_lookback < self.z_lookback:
            raise ConfigError("need z_lookback >= 20 and hedge_lookback >= z_lookback")
        if self.leg_notional <= 0 or self.max_gross_exposure < 2 * self.leg_notional:
            raise ConfigError("max_gross_exposure must be at least 2 x leg_notional")
        if self.max_daily_loss_usd <= 0 or not (0 < self.max_daily_loss_pct < 0.5):
            raise ConfigError("bad daily loss limits")
        if self.data_feed not in ("iex", "sip"):
            raise ConfigError("ALPACA_DATA_FEED must be 'iex' or 'sip'")


def load_settings(**overrides: object) -> Settings:
    """Build Settings from the environment; keyword overrides win (used by tests)."""
    values: dict[str, object] = dict(
        mode=_env("BOT_MODE", "sim").lower(),
        api_key=_env("ALPACA_API_KEY", ""),
        secret_key=_env("ALPACA_SECRET_KEY", ""),
        data_feed=_env("ALPACA_DATA_FEED", "iex").lower(),
        symbol_a=_env("PAIR_SYMBOL_A", "KO").upper(),
        symbol_b=_env("PAIR_SYMBOL_B", "PEP").upper(),
        timeframe_minutes=_i("TIMEFRAME_MINUTES", 1),
        hedge_lookback=_i("HEDGE_LOOKBACK", 390),
        z_lookback=_i("Z_LOOKBACK", 60),
        poll_seconds=_f("POLL_SECONDS", 60.0),
        max_bar_age_seconds=_f("MAX_BAR_AGE_SECONDS", 180.0),
        entry_z=_f("ENTRY_Z", 2.0),
        exit_z=_f("EXIT_Z", 0.5),
        stop_z=_f("STOP_Z", 3.5),
        max_holding_bars=_i("MAX_HOLDING_BARS", 390),
        min_coint_pvalue=_f("MIN_COINT_PVALUE", 0.05),
        leg_notional=_f("LEG_NOTIONAL", 2_000.0),
        max_gross_exposure=_f("MAX_GROSS_EXPOSURE", 10_000.0),
        max_daily_loss_usd=_f("MAX_DAILY_LOSS_USD", 300.0),
        max_daily_loss_pct=_f("MAX_DAILY_LOSS_PCT", 0.02),
        max_consecutive_errors=_i("MAX_CONSECUTIVE_ERRORS", 5),
        db_path=_env("PAIRS_DB_PATH", "pairs_bot.db"),
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
