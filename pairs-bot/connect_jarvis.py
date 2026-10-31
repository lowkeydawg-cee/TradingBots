"""Connect the trading bot to Jarvis in one step, and check that it worked.

    python connect_jarvis.py                          # finds your Jarvis folder by itself
    python connect_jarvis.py --jarvis C:/Users/you/Pac
    python connect_jarvis.py --ssh bot@pairs-server   # the bot runs on your server, Jarvis on this PC
    python connect_jarvis.py --demo                   # no bot running yet: make simulated data to try it

Run it with the same Python you run the bot with (the bot's .venv), from the
pairs-bot folder. It:

1. finds Jarvis's config.yaml (or uses --jarvis);
2. makes sure there is a bot database for Jarvis to read (with --demo it fills
   one with simulated prices so you can try it before the bot runs for real);
3. starts the bot's MCP server exactly the way Jarvis will, and asks it for
   the bot status and the market brief, so you know it works;
4. adds the ``trading`` (questions, no confirmation) and ``trading_control``
   (pause, resume, stop: Jarvis asks first) servers to config.yaml, after
   saving a backup as config.yaml.bak;
5. checks the things on Jarvis's side that would stop it seeing the tools:
   the ``mcp`` package in Jarvis's Python and a brain that can use tools.

Then restart Jarvis. Its startup should print ``[mcp] trading: 7 tools``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
READ_TOOLS = ["get_bot_status", "get_market_brief", "get_market_watch", "explain", "get_recent_trades",
              "get_profit", "get_demo_results"]
CONTROL_TOOLS = ["pause", "resume", "stop_everything"]
MARK = "# --- trading bot (added by pairs-bot/connect_jarvis.py) ---"


def find_jarvis(given: str | None) -> Path | None:
    if given:
        p = Path(given).expanduser()
        return p if (p / "jarvis").is_dir() or (p / "config.yaml").exists() else None
    home = Path.home()
    roots = [HERE.parent.parent, HERE.parent, home, home / "code", home / "Documents", home / "Desktop", home / "projects"]
    names = ["Pac", "pac", "jarvis-assistant", "jarvis", "Jarvis"]
    for root in roots:
        for name in names:
            p = root / name
            if (p / "jarvis" / "mcp_client.py").exists():
                return p
    return None


def server_entries(args) -> tuple[dict, str]:
    """(launch settings for testing, YAML text for config.yaml)."""
    def yq(s: str) -> str:
        return '"' + s.replace("\\", "/").replace('"', '\\"') + '"'
    if args.ssh:
        remote = "cd TradingBots/pairs-bot && docker compose exec -T bot python mcp_server.py"
        launch = {"command": "ssh", "args": [args.ssh, remote], "env": {}}
        body = f"      command: ssh\n      args: [{yq(args.ssh)}, {yq(remote)}]\n"
    else:
        db = str(Path(args.db).resolve())
        launch = {"command": sys.executable, "args": [str(HERE / "mcp_server.py")], "env": {"PAIRS_DB_PATH": db}}
        body = (f"      command: {yq(sys.executable)}\n      args: [{yq(str(HERE / 'mcp_server.py'))}]\n"
                f"      env: {{PAIRS_DB_PATH: {yq(db)}}}\n")
    text = (f"    {MARK}\n"
            f"    trading:\n{body}      confirm: false\n      allow_tools: [{', '.join(READ_TOOLS)}]\n"
            f"    trading_control:\n{body}      confirm: true\n      allow_tools: [{', '.join(CONTROL_TOOLS)}]\n"
            f"    # --- end trading bot ---\n")
    return launch, text


def add_to_config(cfg_path: Path, block: str) -> str:
    """Insert (or replace) our block under mcp.servers, keeping the rest of the file and its comments."""
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    text = re.sub(rf"[ \t]*{re.escape(MARK)}.*?# --- end trading bot ---\n", "", text, flags=re.S)
    lines = text.splitlines(keepends=True)
    mcp = next((i for i, l in enumerate(lines) if re.match(r"^mcp:\s*(#.*)?$", l)), None)
    if mcp is None:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "mcp:\n  servers:\n" + block
    else:
        servers = None
        for i in range(mcp + 1, len(lines)):
            if re.match(r"^\S", lines[i]):           # next top-level key: mcp block ended
                break
            if re.match(r"^\s+servers:", lines[i]):
                servers = i
                break
        if servers is None:
            lines.insert(mcp + 1, "  servers:\n" + block)
        else:
            lines[servers] = re.sub(r"servers:\s*\{\s*\}", "servers:", lines[servers])
            lines.insert(servers + 1, block)
        text = "".join(lines)
    if cfg_path.exists():
        shutil.copy2(cfg_path, cfg_path.with_name(cfg_path.name + ".bak"))
    cfg_path.write_text(text, encoding="utf-8")
    return text


async def try_server(launch: dict) -> tuple[list[str], str, str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=launch["command"], args=launch["args"],
                                   env={**os.environ, **launch["env"]})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [t.name for t in (await s.list_tools()).tools]
            status = (await s.call_tool("get_bot_status", {})).content[0].text
            brief = (await s.call_tool("get_market_brief", {})).content[0].text
    return tools, status, brief


def jarvis_python(jarvis: Path) -> str | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python", "venv/Scripts/python.exe", "venv/bin/python"):
        if (jarvis / rel).exists():
            return str(jarvis / rel)
    return None


def check_jarvis_side(jarvis: Path, cfg_text: str) -> list[str]:
    problems = []
    py = jarvis_python(jarvis)
    if py:
        ok = subprocess.run([py, "-c", "import mcp"], capture_output=True).returncode == 0
        if not ok:
            problems.append(f"Jarvis's Python has no MCP support. Run in the Jarvis folder:\n"
                            f"    {py} -m pip install \"mcp>=1.2\"")
    else:
        problems.append("Couldn't find Jarvis's .venv. In the Python you run Jarvis with, make sure this works:\n"
                        "    python -c \"import mcp\"     (if not: pip install \"mcp>=1.2\")")
    provider = re.search(r"^\s*provider:\s*(\w+)", cfg_text.split("\nstt:")[0], re.M)
    if re.search(r"^\s*local_tools:\s*false", cfg_text, re.M) and (provider is None or provider.group(1) != "cloud"):
        problems.append("local_tools is false: when Groq is unreachable, the local model gets no tools and "
                        "can't see the bot. Keep GROQ_API_KEY set (Jarvis's doctor checks it).")
    if not os.environ.get("GROQ_API_KEY"):
        problems.append("GROQ_API_KEY isn't set in this terminal. If Jarvis can't reach Groq it falls back to the "
                        "local model, which may not use tools. Check with: jarvis --doctor (or python -m jarvis --doctor)")
    return problems


def make_demo_db(db: str) -> None:
    print("  making simulated data (about a minute)...")
    env = {**os.environ, "PAIRS_DB_PATH": db}
    subprocess.run([sys.executable, str(HERE / "watcher.py"), "--source", "sim", "--fast", "--scans", "120"],
                   env=env, check=True, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Connect the trading bot to Jarvis")
    ap.add_argument("--jarvis", help="your Jarvis folder (the one with config.yaml)")
    ap.add_argument("--ssh", help="the bot runs on a server: user@host for SSH (e.g. bot@pairs-server)")
    ap.add_argument("--db", default=os.environ.get("PAIRS_DB_PATH", str(HERE / "pairs_bot.db")))
    ap.add_argument("--demo", action="store_true", help="no bot data yet: fill the database with a simulated run")
    ap.add_argument("--dry-run", action="store_true", help="test only, don't change config.yaml")
    args = ap.parse_args(argv)

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("This Python has no MCP support. Run:  pip install -r requirements.txt   then try again.")
        return 1

    jarvis = find_jarvis(args.jarvis)
    if jarvis is None:
        print("Couldn't find your Jarvis folder. Run again with --jarvis followed by its path,\n"
              "for example:  python connect_jarvis.py --jarvis C:/Users/you/Pac")
        return 1
    print(f"1. Jarvis found: {jarvis}")

    if not args.ssh:
        if not Path(args.db).exists():
            if args.demo:
                make_demo_db(args.db)
            else:
                print(f"2. No bot database at {args.db}.\n   Start the bot (python main.py) or the watcher "
                      "(python watcher.py), or run again with --demo to try it on simulated data.")
                return 1
        print(f"2. Bot database: {args.db}")

    launch, block = server_entries(args)
    try:
        tools, status, brief = asyncio.run(asyncio.wait_for(try_server(launch), 90))
    except Exception as exc:
        print(f"3. The bot's MCP server didn't answer: {exc!r}")
        if args.ssh:
            print("   Check you can log in without a password:  ssh " + args.ssh + "  (set up an SSH key first)")
        return 1
    print(f"3. The bot answered with {len(tools)} tools. Status: {status.splitlines()[0]}")
    print("   " + brief.splitlines()[1] if len(brief.splitlines()) > 1 else "   " + brief)

    cfg_path = jarvis / "config.yaml"
    if not cfg_path.exists() and (jarvis / "config.example.yaml").exists():
        shutil.copy2(jarvis / "config.example.yaml", cfg_path)
        print("   (made config.yaml from config.example.yaml)")
    if args.dry_run:
        print("4. Dry run: config.yaml not changed. This is what would be added under mcp.servers:\n" + block)
        cfg_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    else:
        cfg_text = add_to_config(cfg_path, block)
        print(f"4. Added the trading bot to {cfg_path} (backup: config.yaml.bak)")

    problems = check_jarvis_side(jarvis, cfg_text)
    if problems:
        print("5. Fix these on Jarvis's side:")
        for p in problems:
            print("   - " + p)
    else:
        print("5. Jarvis's side looks ready.")
    print("\nNow restart Jarvis. Its startup should print:  [mcp] trading: 7 tools\n"
          "Then ask:  \"how does the market look?\"  or  \"how's the trading bot?\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
