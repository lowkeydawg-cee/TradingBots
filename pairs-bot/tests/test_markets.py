"""Beyond US stocks: crypto, world funds, the 'best now' ranking and the US open/close switch."""

import asyncio
from datetime import datetime

import pandas as pd

import watcher as w
from insights import NY, Insights, day_moves, read_market, risk_read
from scanner import ALL_SECTORS, CRYPTO, MARKET_OF, UNIVERSES, alpaca_crypto


def opp(pair, grade, sector, vol=1.0, p=0.01):
    return w.Opportunity(pair=pair, sector=sector, bar_time=0, grade=grade, side=None, zscore=0.0, coint_pvalue=p,
                         half_life=10, vol_ratio=vol, beta=1, hist_trades=10, hist_win_rate=0.6, prob=0.6,
                         hist_avg_net=1.0, breakeven_bps=5.0, flags=[])


def test_universe_covers_crypto_gold_currencies_and_world():
    syms = {s for v in ALL_SECTORS.values() for s in v}
    assert {"BTCUSD", "ETHUSD", "GLD", "SLV", "FXE", "FXY", "UUP", "TLT", "USO", "VGK", "EWJ", "SPY"} <= syms
    assert set(MARKET_OF) == set(ALL_SECTORS)
    assert "crypto" not in UNIVERSES and alpaca_crypto("BTCUSD") == "BTC/USD"
    assert "|" not in "".join(syms) and all("/" not in s for s in syms)   # pair names split on "/"


def test_day_moves_measures_each_symbol_on_its_own_last_session():
    idx = pd.DatetimeIndex([datetime(2026, 11, 2, 15, 45, tzinfo=NY), datetime(2026, 11, 3, 15, 45, tzinfo=NY),
                            datetime(2026, 11, 4, 2, 0, tzinfo=NY)])       # 2am: only crypto has a price
    closes = pd.DataFrame({"SPY": [100.0, 102.0, None], "BTCUSD": [100.0, 100.0, 95.0]}, index=idx)
    m = day_moves(closes)
    assert round(m["SPY"], 4) == 0.02 and round(m["BTCUSD"], 4) == -0.05


def test_risk_read():
    assert risk_read(pd.Series({"SPY": 0.01, "TLT": -0.004, "GLD": -0.002})).startswith("risk-on")
    assert risk_read(pd.Series({"SPY": -0.012, "GLD": 0.01})).startswith("risk-off")
    assert risk_read(pd.Series({"SPY": 0.001, "GLD": 0.0})).startswith("mixed")
    assert risk_read(pd.Series({"SPY": 0.01})) is None


def test_brief_ranks_markets_and_shows_the_world():
    idx = pd.DatetimeIndex([datetime(2026, 11, 2, 15, 45, tzinfo=NY), datetime(2026, 11, 3, 10, 0, tzinfo=NY)])
    closes = pd.DataFrame({"SPY": [100, 101.0], "GLD": [100, 99.5], "TLT": [100, 99.8], "BTCUSD": [100, 103.0],
                           "ETHUSD": [100, 104.0], "KO": [100, 100.0], "PEP": [100, 100.0]}, index=idx)
    opps = [opp("BTCUSD/ETHUSD", "STRONG", "crypto"), opp("BTCUSD/SOLUSD", "WATCH", "crypto"),
            opp("GLD/SLV", "AVOID", "metals", p=0.5), opp("GDX/GDXJ", "QUIET", "metals", p=0.5),
            opp("KO/PEP", "WATCH", "beverages", vol=2.5), opp("JPM/BAC", "AVOID", "banks", vol=2.5)]
    r = read_market(opps, closes, datetime(2026, 11, 3, 10, 0, tzinfo=NY).timestamp())
    assert [m.name for m in r.markets][0] == "Crypto" and r.markets[0].label == "good"
    assert {m.name: m.label for m in r.markets}["Gold & metals"] == "poor"
    text = r.text()
    assert "Best now: 1. Crypto good (2/2 pairs healthy, normal, 1 strong signal)" in text
    assert "bitcoin +3.0%" in text and "gold -0.5%" in text and "risk-on" in text
    assert "stocks and funds -0.2%" in text          # GLD, KO, PEP only: crypto's +3% is kept out


def test_switching_to_crypto_only_at_the_close_does_not_fire_market_alerts():
    ins = Insights()
    t = datetime(2026, 11, 3, 15, 0, tzinfo=NY).timestamp()
    day = [opp("KO/PEP", "WATCH", "beverages"), opp("XOM/CVX", "WATCH", "energy"),
           opp("BTCUSD/ETHUSD", "WATCH", "crypto", vol=2.0, p=0.5)]
    ins.update(day, pd.DataFrame(), t)
    night = [opp("BTCUSD/ETHUSD", "WATCH", "crypto", vol=2.0, p=0.5)]
    read, alerts = ins.update(night, pd.DataFrame(), t + 3600, closed=["US stocks"])
    assert alerts == []                                # mood jumped and coint share fell, but only the mix changed
    assert read.closed == ["US stocks"]
    _, alerts = ins.update(night, pd.DataFrame(), t + 7200, closed=["US stocks"])
    assert alerts == []


def test_crypto_pairs_use_crypto_costs_and_only_open_markets_are_scanned():
    assert w.CRYPTO_COSTS.borrow_rate_annual == 0 and w.CRYPTO_COSTS.spread_bps > w.Costs().spread_bps

    class Src:
        sectors = {"crypto": CRYPTO["crypto"][:2], "banks": ["JPM", "BAC"]}
        active_sectors = {"crypto": CRYPTO["crypto"][:2]}
    watcher = w.Watcher.__new__(w.Watcher)
    watcher.source, watcher.costs = Src(), w.Costs()
    assert watcher.pairs() == [(("BTCUSD", "ETHUSD"), "crypto")]
    assert len(watcher.pairs(every=True)) == 2
    assert watcher.costs_for("crypto") is w.CRYPTO_COSTS and watcher.costs_for("banks") is watcher.costs


def test_alpaca_source_scans_crypto_when_the_us_market_is_closed(monkeypatch):
    src = w.AlpacaSource.__new__(w.AlpacaSource)
    src.sectors = {"banks": ["JPM", "BAC"], "crypto": ["BTCUSD", "ETHUSD"]}
    src.stock_sectors, src.crypto_sectors = {"banks": ["JPM", "BAC"]}, {"crypto": ["BTCUSD", "ETHUSD"]}
    src._stock_cache = None

    class Clock:
        is_open = False
    src._trading = type("T", (), {"get_clock": staticmethod(lambda: Clock())})()
    idx = pd.date_range("2026-11-03 14:00", periods=3, freq="15min", tz="UTC")
    calls = []
    src._fetch_stocks = lambda bars: calls.append("stocks") or pd.DataFrame({"JPM": [1.0] * 3, "BAC": [1.0] * 3}, idx)
    src._fetch_crypto = lambda bars: calls.append("crypto") or pd.DataFrame({"BTCUSD": [1.0] * 3, "ETHUSD": [1.0] * 3}, idx)

    assert asyncio.run(src.market_open()) is True          # crypto never closes
    assert src.active_sectors == {"crypto": ["BTCUSD", "ETHUSD"]} and src.closed_markets == ["US stocks"]
    df = src._fetch(10)
    assert set(df.columns) == {"JPM", "BAC", "BTCUSD", "ETHUSD"}
    src._fetch(10)
    assert calls == ["stocks", "crypto", "crypto"]           # last session's stock prices are reused while closed
    Clock.is_open = True
    asyncio.run(src.market_open())
    assert set(src.active_sectors) == {"banks", "crypto"} and src.closed_markets == []
