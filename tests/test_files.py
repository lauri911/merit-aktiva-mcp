"""Files for the tools that take one: a local path in local mode, an upload link on a server."""

import pytest
from fastmcp.exceptions import ToolError

from merit_aktiva_mcp import files


class FakeUploads:
    def get(self, upload_id):
        return "bill.pdf", b"%PDF-1.4 x", "application/pdf"


def test_local_path_is_read(tmp_path):
    f = tmp_path / "bill.pdf"
    f.write_bytes(b"%PDF-1.4 hello")
    assert files.LocalFiles().get(None, str(f), (".pdf",)) == ("bill.pdf", b"%PDF-1.4 hello")


def test_local_path_refusals(tmp_path, monkeypatch):
    local = files.LocalFiles()
    with pytest.raises(ToolError, match="no file"):
        local.get(None, str(tmp_path / "missing.pdf"), (".pdf",))
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ToolError, match=r"must be a \.pdf"):
        local.get(None, str(tmp_path / "a.txt"), (".pdf",))
    monkeypatch.setattr(files, "MAX_BYTES", 10)
    (tmp_path / "big.pdf").write_bytes(b"x" * 11)
    with pytest.raises(ToolError, match="over"):
        local.get(None, str(tmp_path / "big.pdf"), (".pdf",))
    with pytest.raises(ToolError, match="upload links exist only on a server"):
        local.get("up_1", None, (".pdf",))
    with pytest.raises(ToolError, match="give file_path"):
        local.get(None, None, (".pdf",))


def test_server_takes_uploads_only():
    server = files.ServerFiles(FakeUploads())
    assert server.get("up_1", None, (".pdf",)) == ("bill.pdf", b"%PDF-1.4 x")
    with pytest.raises(ToolError, match="cannot read your disk"):
        server.get(None, "/etc/passwd", (".pdf",))
