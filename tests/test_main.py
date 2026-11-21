"""How the server is put together for each setting: read tools always, write tools only with
MERIT_ALLOW_WRITE, upload links only on a server, and instructions without anyone's private data."""

import asyncio
import dataclasses
import re
from pathlib import Path

import pytest
from fastmcp import Client

from merit_aktiva_mcp import auth, config, main

SRC = Path(main.__file__).parent


def cfg(**changes) -> config.Config:
    base = config.Config(api_id="id", api_key="key", base_url=config.DEFAULT_BASE_URL, allow_write=False,
                         extra_instructions="", transport="stdio", public_url=None, auth_provider=None,
                         client_id=None, client_secret=None, tenant_id=None, allowed_emails=frozenset(),
                         jwt_signing_key=None)
    return dataclasses.replace(base, **changes)


def tools(c: config.Config) -> list:
    mcp = main.build(c)

    async def run():
        async with Client(mcp) as client:
            return await client.list_tools()
    return asyncio.run(run())


def test_read_only_lists_no_write_tools():
    listed = tools(cfg())
    assert listed and all(t.annotations.read_only_hint for t in listed)
    assert "merit_create_sales_invoice" not in {t.name for t in listed}


def test_write_tools_have_dry_run_and_destructive_hint():
    for t in tools(cfg(allow_write=True)):
        if t.annotations.read_only_hint:
            assert "dry_run" not in t.input_schema["properties"], t.name
        else:
            assert t.annotations.destructive_hint, t.name
            assert "dry_run" in t.input_schema["properties"], t.name


def test_every_tool_registered_once():
    names = [t.name for t in tools(cfg(allow_write=True))]
    defined = set()
    for f in SRC.glob("merit*.py"):
        defined |= set(re.findall(r"async def (merit_\w+)\(", f.read_text()))
    assert len(names) == len(set(names))
    assert sorted(defined) == sorted(n for n in names if n.startswith("merit_"))


def test_upload_link_only_on_a_server(monkeypatch):
    monkeypatch.setattr(auth, "build_auth", lambda c: None)
    assert "create_upload_link" not in {t.name for t in tools(cfg(allow_write=True))}
    server = cfg(allow_write=True, transport="http", public_url="https://merit.example.com")
    assert "create_upload_link" in {t.name for t in tools(server)}


def test_extra_instructions_appended():
    text = main.instructions(cfg(extra_instructions="Customers abroad are invoiced in EUR."))
    assert text.rstrip().endswith("Customers abroad are invoiced in EUR.")
    assert "Read only" in main.instructions(cfg()) and "dry_run=true" in main.instructions(cfg(allow_write=True))


def test_instructions_name_only_this_servers_tools():
    named = set(re.findall(r"\bmerit_\w+", main.instructions(cfg(allow_write=True))))
    real = {t.name for t in tools(cfg(allow_write=True))}
    assert named
    for name in named:  # "merit_find_*" names a group of tools
        assert name in real or (name.endswith("_") and any(r.startswith(name) for r in real)), name
