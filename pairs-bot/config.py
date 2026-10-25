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

import dataclasses
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

    # --- the pair(s) -----------------------------------------------------------
    # Spread = log(A) - beta * log(B) - alpha. Classic, liquid, economically linked pair.
    symbol_a: str = "KO"
    symbol_b: str = "PEP"
    # Portfolio mode: several pairs traded side by side, e.g. (("KO","PEP"),("XOM","CVX")).
    # Empty = just (symbol_a, symbol_b). Pairs may not share a symbol.
    pairs: tuple[tuple[str, str], ...] = ()

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

    # --- model -------------------------------------------------------------------
    hedge_method: str = "ols"               # "ols" (rolling regression) or "kalman" (adaptive beta)
    kalman_delta: float = 1e-3              # how fast the Kalman beta may drift (bigger = faster)
    kalman_obs_var: float = 0.0             # Kalman measurement noise; 0 = estimate from the window
    max_vol_ratio: float = 3.0              # block entries if recent spread volatility > this x normal
    blackout_file: str = "blackout.csv"     # earnings/news dates: no entries, close open trades
    news_pause_minutes: int = 30            # alpaca mode: no entries if a symbol had news this recently (0 = off)

    # --- sizing and risk limits --------------------------------------------------
    leg_notional: float = 2_000.0           # $ exposure on the A leg; B leg is beta-weighted
    max_gross_exposure: float = 10_000.0    # |A| + |B| market value cap
    max_daily_loss_usd: float = 300.0       # vs. previous close equity
    max_daily_loss_pct: float = 0.02        # 2% of previous close equity; the tighter of the two wins
    max_consecutive_errors: int = 5         # API/data errors in a row before halting
    risk_per_trade_usd: float = 0.0         # >0: size each pair so a stop-out loses about this much
    max_weekly_loss_pct: float = 0.04       # halt if down this much since the week's first close
    max_monthly_loss_pct: float = 0.08      # halt if down this much since the month's first close
    retire_pvalue: float = 0.10             # a pair whose daily cointegration p-value stays above this...
    retire_days: int = 5                    # ...for this many trading days in a row stops opening trades

    # --- execution -----------------------------------------------------------------
    order_type: str = "limit"               # "limit": passive first leg at the mid; "market": old behaviour
    limit_timeout_seconds: float = 20.0     # how long the passive leg may rest before it is cancelled
    max_slippage_bps: float = 10.0          # cap for the marketable second leg, vs. the quote
    max_quote_spread_bps: float = 15.0      # skip entries when the bid/ask spread is wider than this

    # --- operations ----------------------------------------------------------------
    data_source: str = "poll"               # "poll" (REST every bar) or "stream" (Alpaca websocket)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    notify_trades: bool = True              # also send a message on each open/close (halts always alert)

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
        if self.hedge_method not in ("ols", "kalman"):
            raise ConfigError("HEDGE_METHOD must be 'ols' or 'kalman'")
        if self.order_type not in ("limit", "market"):
            raise ConfigError("ORDER_TYPE must be 'limit' or 'market'")
        if self.data_source not in ("poll", "stream"):
            raise ConfigError("DATA_SOURCE must be 'poll' or 'stream'")
        symbols = [s for pair in self.all_pairs for s in pair]
        if any(a == b for a, b in self.all_pairs):
            raise ConfigError("the two symbols of a pair must differ")
        if len(symbols) != len(set(symbols)):
            raise ConfigError("pairs may not share a symbol (closing one would close the other's leg)")

    @property
    def all_pairs(self) -> tuple[tuple[str, str], ...]:
        return self.pairs or ((self.symbol_a, self.symbol_b),)

    def for_pair(self, a: str, b: str) -> "Settings":
        """The same settings, focused on one pair (each pair engine gets its own view)."""
        return dataclasses.replace(self, symbol_a=a, symbol_b=b, pairs=self.all_pairs)


def _pairs(raw: str) -> tuple[tuple[str, str], ...]:
    """'KO:PEP, XOM:CVX' -> (("KO","PEP"), ("XOM","CVX"))."""
    out = []
    for item in filter(None, (x.strip() for x in raw.split(","))):
        a, sep, b = item.partition(":")
        if not sep:
            raise ConfigError(f"bad PAIRS entry {item!r}; use A:B")
        out.append((a.strip().upper(), b.strip().upper()))
    return tuple(out)


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
        pairs=_pairs(_env("PAIRS", "")),
        hedge_method=_env("HEDGE_METHOD", "ols").lower(),
        kalman_delta=_f("KALMAN_DELTA", 1e-3),
        kalman_obs_var=_f("KALMAN_OBS_VAR", 0.0),
        max_vol_ratio=_f("MAX_VOL_RATIO", 3.0),
        blackout_file=_env("BLACKOUT_FILE", "blackout.csv"),
        news_pause_minutes=_i("NEWS_PAUSE_MINUTES", 30),
        risk_per_trade_usd=_f("RISK_PER_TRADE_USD", 0.0),
        max_weekly_loss_pct=_f("MAX_WEEKLY_LOSS_PCT", 0.04),
        max_monthly_loss_pct=_f("MAX_MONTHLY_LOSS_PCT", 0.08),
        retire_pvalue=_f("RETIRE_PVALUE", 0.10),
        retire_days=_i("RETIRE_DAYS", 5),
        order_type=_env("ORDER_TYPE", "limit").lower(),
        limit_timeout_seconds=_f("LIMIT_TIMEOUT_SECONDS", 20.0),
        max_slippage_bps=_f("MAX_SLIPPAGE_BPS", 10.0),
        max_quote_spread_bps=_f("MAX_QUOTE_SPREAD_BPS", 15.0),
        data_source=_env("DATA_SOURCE", "poll").lower(),
        telegram_token=_env("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
        notify_trades=_env("NOTIFY_TRADES", "1") not in ("0", "false", "no"),
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
