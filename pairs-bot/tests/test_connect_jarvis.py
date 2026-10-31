"""connect_jarvis.py edits Jarvis's config.yaml safely and produces something Jarvis can load."""

import types

import yaml

import connect_jarvis as cj


def block():
    return cj.server_entries(types.SimpleNamespace(ssh=None, db="pairs_bot.db"))[1]


def servers(text):
    return yaml.safe_load(text)["mcp"]["servers"]


def test_adds_to_existing_servers_and_keeps_comments(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm:\n  provider: hybrid  # keep me\nmcp:\n  servers:\n    git:\n      command: uvx\n"
                   "proactive:\n  enabled: true\n")
    text = cj.add_to_config(cfg, block())
    s = servers(text)
    assert set(s) == {"trading", "trading_control", "git"}
    assert s["trading"]["confirm"] is False and s["trading_control"]["confirm"] is True
    assert "get_market_brief" in s["trading"]["allow_tools"] and s["trading_control"]["allow_tools"] == cj.CONTROL_TOOLS
    assert "# keep me" in text and yaml.safe_load(text)["proactive"]["enabled"] is True
    assert (tmp_path / "config.yaml.bak").exists()
    again = cj.add_to_config(cfg, block())                     # running twice doesn't duplicate
    assert again.count("trading_control:") == 1 and set(servers(again)) == set(s)


def test_handles_empty_and_missing_mcp_sections(tmp_path):
    a = tmp_path / "a.yaml"
    a.write_text("mcp:\n  servers: {}\n")
    assert set(servers(cj.add_to_config(a, block()))) == {"trading", "trading_control"}
    b = tmp_path / "b.yaml"
    b.write_text("web:\n  port: 8765\n")
    t = cj.add_to_config(b, block())
    assert set(servers(t)) == {"trading", "trading_control"} and yaml.safe_load(t)["web"]["port"] == 8765


def test_ssh_mode_runs_the_server_inside_the_container():
    launch, text = cj.server_entries(types.SimpleNamespace(ssh="bot@pairs-server", db="x"))
    s = yaml.safe_load("mcp:\n  servers:\n" + text)["mcp"]["servers"]
    assert s["trading"]["command"] == "ssh" and "docker compose exec -T bot python mcp_server.py" in s["trading"]["args"][1]
    assert launch["command"] == "ssh"


def test_finds_a_jarvis_folder_named_pac(tmp_path):
    (tmp_path / "Pac" / "jarvis").mkdir(parents=True)
    (tmp_path / "Pac" / "jarvis" / "mcp_client.py").write_text("")
    assert cj.find_jarvis(str(tmp_path / "Pac")) == tmp_path / "Pac"
