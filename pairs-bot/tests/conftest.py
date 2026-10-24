import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402
from db_logger import DBLogger  # noqa: E402
from main import PairsBot  # noqa: E402
from sim import SimBroker, SimMarket, SimMarketData  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    def _make(**overrides):
        base = dict(mode="sim", poll_seconds=0.0, order_type="market", hedge_lookback=200, z_lookback=40,
                    db_path=str(tmp_path / "pairs.db"))
        base.update(overrides)
        return load_settings(**base)
    return _make


@pytest.fixture
def make_bot(cfg):
    """Build a bot on a warmed-up simulated market. Returns (bot, market, broker)."""
    def _make(market_kw=None, broker_kw=None, **cfg_overrides):
        c = cfg(**cfg_overrides)
        market = SimMarket(symbol_a=c.symbol_a, symbol_b=c.symbol_b, **(market_kw or {}))
        market.warm_up(c.hedge_lookback)
        broker = SimBroker(market, **(broker_kw or {}))
        bot = PairsBot(c, SimMarketData(market), broker, DBLogger(c.db_path), clock=lambda: market.last[0])
        return bot, market, broker
    return _make
