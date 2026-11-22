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
                         extra_instructions="", company_name=None, transport="stdio", public_url=None, auth_provider=None,
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


def test_company_name_in_server_name_and_instructions():
    named = cfg(company_name="Kask ja Kuusk OÜ")
    assert main.build(named).name == "Merit Aktiva – Kask ja Kuusk OÜ"
    assert main.instructions(named).startswith("These are the books of Kask ja Kuusk OÜ, and only theirs")
    assert main.build(cfg()).name == "Merit Aktiva" and "books of" not in main.instructions(cfg())


def call(c: config.Config, tool: str, args: dict):
    async def run():
        async with Client(main.build(c)) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(run())


def test_company_named_in_write_previews_and_not_in_reads(monkeypatch):
    from merit_aktiva_mcp import merit

    async def no_rows(self, path, payload=None, method="POST", text_ok=False, **kw):
        return []
    monkeypatch.setattr(merit.Merit, "call", no_rows)
    named = cfg(allow_write=True, company_name="Kask ja Kuusk OÜ")

    preview = call(named, "merit_create_unit", {"name": "kast", "dry_run": True})
    assert list(preview.structured_content)[0] == "company"
    assert preview.structured_content["company"] == "Kask ja Kuusk OÜ"
    assert preview.content[0].text.startswith('{"company":"Kask ja Kuusk OÜ"')
    assert preview.structured_content["preview"] == {"new_unit": {"Name": "kast"}}

    unnamed = call(cfg(allow_write=True), "merit_create_unit", {"name": "kast", "dry_run": True})
    assert "company" not in unnamed.structured_content
    read = call(named, "merit_reference_list", {"kind": "units"})
    assert "company" not in (read.structured_content or {})


def test_company_named_in_written_results_and_refusals_only(monkeypatch):
    from fastmcp.exceptions import ToolError
    from merit_aktiva_mcp import merit
    units: list = []

    async def fake_call(self, path, payload=None, method="POST", text_ok=False, **kw):
        return {"Id": "11111111-2222-3333-4444-555555555555"} if path == "v2/senduom" else units
    monkeypatch.setattr(merit.Merit, "call", fake_call)
    named = cfg(allow_write=True, company_name="Kask ja Kuusk OÜ")

    written = call(named, "merit_create_unit", {"name": "kast"}).structured_content
    assert list(written)[0] == "company" and written["written"] is True

    units.append({"Code": "kast", "Name": "kast"})
    with pytest.raises(ToolError, match="^Kask ja Kuusk OÜ: unit 'kast' already exists"):
        call(named, "merit_create_unit", {"name": "kast"})

    monkeypatch.setattr(auth, "build_auth", lambda c: None)
    server = dataclasses.replace(named, transport="http", public_url="https://merit.example.com")
    assert "company" not in call(server, "create_upload_link", {}).structured_content
