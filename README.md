# Trading bots

| Folder | What it is | Status |
|---|---|---|
| [`pairs-bot/`](pairs-bot/) | Async pairs-trading bot on the Alpaca **paper** API, with SQLite logging and a Streamlit dashboard | Tested offline (20 tests); Alpaca connection still to be smoke-tested |
| [`risk-bot/`](risk-bot/) | Risk-governance and logging engine for crash-style game feeds: mock feed and paper executor only | 42 tests; offline simulation only |

Start with [`pairs-bot/GUIDE.md`](pairs-bot/GUIDE.md) for a plain-language explanation, an honest rating and the roadmap.
