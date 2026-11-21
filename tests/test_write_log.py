"""Every write is logged, with who asked for it when someone is signed in (server mode)."""

import logging
from types import SimpleNamespace

from merit_aktiva_mcp import merit_write


def test_write_log_names_the_signed_in_person(monkeypatch, caplog):
    monkeypatch.setattr(merit_write, "get_access_token",
                        lambda: SimpleNamespace(claims={"email": "anna@example.com"}))
    with caplog.at_level(logging.INFO, logger="merit_aktiva_mcp.merit_write"):
        assert merit_write._gate("create_unit", {"Name": "kuu"}, False, {}) is None
    assert "WRITE create_unit by anna@example.com" in caplog.text


def test_dry_run_is_not_logged_as_a_write(caplog):
    with caplog.at_level(logging.INFO, logger="merit_aktiva_mcp.merit_write"):
        out = merit_write._gate("create_unit", {"Name": "kuu"}, True, {"new_unit": {"Name": "kuu"}})
    assert out["written"] is False and "WRITE" not in caplog.text


def test_local_writes_are_logged_as_local(caplog):
    with caplog.at_level(logging.INFO, logger="merit_aktiva_mcp.merit_write"):
        merit_write._gate("create_unit", {"Name": "kuu"}, False, {})
    assert "WRITE create_unit by local" in caplog.text
