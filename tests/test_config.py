"""Settings come from environment variables; any secret may also be a file named by NAME_FILE."""

import asyncio

import pytest
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp import config
from merit_aktiva_mcp.merit import Merit


def test_setting_reads_env_then_file(monkeypatch, tmp_path):
    f = tmp_path / "key"
    f.write_text("from-file\n")
    monkeypatch.setenv("MERIT_API_KEY_FILE", str(f))
    monkeypatch.delenv("MERIT_API_KEY", raising=False)
    assert config.setting("MERIT_API_KEY") == "from-file"
    monkeypatch.setenv("MERIT_API_KEY", "from-env")
    assert config.setting("MERIT_API_KEY") == "from-env"
    assert config.setting("NOT_SET_ANYWHERE", "dflt") == "dflt"


def test_load_defaults(monkeypatch):
    for name in ("MERIT_BASE_URL", "MERIT_ALLOW_WRITE", "MCP_TRANSPORT", "ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    cfg = config.load()
    assert cfg.base_url == "https://aktiva.merit.ee/api"
    assert cfg.allow_write is False
    assert cfg.transport == "stdio"
    assert cfg.allowed_emails == frozenset()


def test_allowed_emails_and_write_switch(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", " Anna@Example.com, bob@example.com ,")
    monkeypatch.setenv("MERIT_ALLOW_WRITE", "TRUE")
    cfg = config.load()
    assert cfg.allowed_emails == {"anna@example.com", "bob@example.com"}
    assert cfg.allow_write is True


def test_extra_instructions_from_file(monkeypatch, tmp_path):
    f = tmp_path / "extra.md"
    f.write_text("Our customers are all in Tartu.")
    monkeypatch.delenv("MERIT_EXTRA_INSTRUCTIONS", raising=False)
    monkeypatch.setenv("MERIT_EXTRA_INSTRUCTIONS_FILE", str(f))
    assert config.load().extra_instructions == "Our customers are all in Tartu."


def test_missing_key_gives_clear_error_at_call_time():
    m = Merit("https://aktiva.merit.ee/api", None, None)
    with pytest.raises(ToolError, match="MERIT_API_ID and MERIT_API_KEY"):
        asyncio.run(m.call("v2/banks", None, method="GET"))


def test_unfilled_extension_placeholder_counts_as_unset(monkeypatch):
    # Claude Desktop extensions map optional fields as ${user_config.x}; left empty, that must not leak.
    monkeypatch.setenv("MERIT_EXTRA_INSTRUCTIONS", "${user_config.extra_instructions}")
    monkeypatch.delenv("MERIT_EXTRA_INSTRUCTIONS_FILE", raising=False)
    assert config.setting("MERIT_EXTRA_INSTRUCTIONS") is None


def test_missing_secret_file_stops_with_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.delenv("MERIT_API_KEY", raising=False)
    monkeypatch.setenv("MERIT_API_KEY_FILE", str(tmp_path / "nope"))
    with pytest.raises(SystemExit, match="MERIT_API_KEY_FILE"):
        config.setting("MERIT_API_KEY")
