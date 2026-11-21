"""An upload link (server mode) refuses oversized files."""

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from merit_aktiva_mcp import uploads


def test_upload_over_the_limit_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(uploads, "MAX_BYTES", 1000)
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path)
    up = uploads.Uploads("https://x")
    app = Starlette(routes=[Route("/upload/{token}", up.endpoint, methods=["GET", "POST", "PUT"])])
    client = TestClient(app)
    big = up.create_link()
    r = client.post(big["upload_url"].replace("https://x", ""), content=b"x" * 2000)
    assert r.status_code == 413 and not list(tmp_path.iterdir())
    small = up.create_link()
    r = client.post(small["upload_url"].replace("https://x", "") + "?name=a.pdf", content=b"%PDF-1.4 small")
    assert r.status_code == 200 and up.get(small["upload_id"])[1] == b"%PDF-1.4 small"
