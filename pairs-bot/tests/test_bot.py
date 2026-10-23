import asyncio
import sqlite3
import threading

from db_logger import connect_readonly, request_emergency_stop
from execution import Account
from main import PairsBot
from sim import SimMarketData
from db_logger import DBLogger


def rows(db, sql):
    with sqlite3.connect(db) as c:
        return c.execute(sql).fetchall()


async def test_end_to_end_trades_and_logs_everything(make_bot):
    bot, _, broker = make_bot()
    await bot.start()
    await bot.run(max_cycles=600)
    await bot.db.close()
    db = bot.cfg.db_path
    assert rows(db, "select count(*) from price_checks")[0][0] == 600
    assert rows(db, "select count(*) from signals")[0][0] == 600
    assert rows(db, "select count(*) from equity")[0][0] == 600
    closed = rows(db, "select pnl from pair_trades where closed_at is not null")
    assert len(closed) >= 2
    # Every closed trade has exactly 2 opening and 2 closing orders in the log.
    n_orders = rows(db, "select count(*) from orders")[0][0]
    open_now = rows(db, "select count(*) from pair_trades where closed_at is null")[0][0]
    assert n_orders == 4 * len(closed) + 2 * open_now
    assert len(broker.orders) == n_orders


async def test_daily_loss_limit_flattens_and_halts(make_bot):
    bot, market, broker = make_bot(max_daily_loss_usd=50)
    await bot.start()
    for _ in range(400):                      # run until a position is open
        await bot.cycle()
        if bot.trade:
            break
    assert bot.trade is not None
    real_account = broker.account

    async def losing_account():
        a = await real_account()
        return Account(a.last_equity - 60, a.last_equity, a.cash, False)
    broker.account = losing_account
    await bot.cycle()
    assert bot.halted == "daily loss limit"
    assert all(q == 0 for q in broker.qty.values())
    reason = rows(bot.cfg.db_path, "select exit_reason from pair_trades order by id desc limit 1")[0][0]
    assert reason.startswith("halt: daily loss limit")
    await bot.db.close()


async def test_dashboard_stop_button_flattens_and_halt_survives_restart(make_bot):
    bot, market, broker = make_bot()
    await bot.start()
    for _ in range(400):
        await bot.cycle()
        if bot.trade:
            break
    request_emergency_stop(bot.cfg.db_path, "test")   # what the Streamlit button does
    await bot.cycle()
    assert bot.halted.startswith("emergency stop")
    assert all(q == 0 for q in broker.qty.values())
    await bot.db.close()

    again = PairsBot(bot.cfg, SimMarketData(market), broker, DBLogger(bot.cfg.db_path),
                     clock=lambda: market.last[0])
    await again.start()
    assert again.halted                                # restart does not resume trading
    await again.db.close()

    reset = PairsBot(bot.cfg, SimMarketData(market), broker, DBLogger(bot.cfg.db_path),
                     clock=lambda: market.last[0])
    await reset.start(reset_halt=True)
    assert reset.halted is None
    await reset.db.close()


async def test_broken_relationship_hits_stop_or_is_blocked(make_bot):
    bot, _, _ = make_bot(market_kw={"break_at": 260, "sigma_s": 0.004})
    await bot.start()
    await bot.run(max_cycles=800)
    await bot.db.close()
    reasons = {r[0] for r in rows(bot.cfg.db_path, "select exit_reason from pair_trades where closed_at is not null")}
    notes = [r[0] for r in rows(bot.cfg.db_path, "select note from signals where action='BLOCKED'")]
    assert any(r.startswith(("stop", "time stop")) for r in reasons) or any("cointegration" in n for n in notes)


async def test_stale_data_blocks_trading(make_bot):
    bot, market, broker = make_bot()
    bot.clock = lambda: market.last[0] + 10_000      # pretend the last bar is hours old
    await bot.start()
    await bot.run(max_cycles=300)
    await bot.db.close()
    assert broker.orders == []
    assert rows(bot.cfg.db_path, "select count(*) from signals where note like 'stale data%'")[0][0] == 300


async def test_repeated_errors_halt(make_bot):
    bot, _, _ = make_bot(max_consecutive_errors=3)

    async def broken(*a, **k):
        raise ConnectionError("data API down")
    bot.data.get_closes = broken
    await bot.start()
    await bot.run(max_cycles=10)
    await bot.db.close()
    assert bot.halted == "3 consecutive errors"


async def test_unknown_positions_at_startup_halt_without_trading(make_bot):
    bot, market, broker = make_bot()
    await broker.market_order(bot.cfg.symbol_a, 5, "buy", "manual")
    await bot.start()
    assert bot.halted == "unknown positions in pair symbols at startup"
    assert broker.qty[bot.cfg.symbol_a] == 5          # left untouched for a human to review
    await bot.db.close()


async def test_dashboard_reads_while_bot_writes(make_bot):
    """WAL + busy_timeout: a reader thread hammering the DB never blocks the bot."""
    bot, _, _ = make_bot()
    await bot.start()
    stop, errors, reads = threading.Event(), [], [0]

    def reader():
        while not stop.is_set():
            try:
                c = connect_readonly(bot.cfg.db_path)
                c.execute("select count(*) from signals").fetchone()
                c.execute("select * from equity order by id desc limit 200").fetchall()
                c.close()
                reads[0] += 1
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
    t = threading.Thread(target=reader)
    t.start()
    try:
        await bot.run(max_cycles=300)
        await asyncio.to_thread(request_emergency_stop, bot.cfg.db_path, "concurrent write")
    finally:
        stop.set()
        t.join()
    await bot.cycle()
    await bot.db.close()
    assert not errors and reads[0] > 10
    assert bot.db.consecutive_failures == 0
    assert bot.halted.startswith("emergency stop")
