"""Getting a file (e.g. a vendor's PDF invoice) from Claude's side into the server without passing it as a tool parameter.

A tool parameter has to be written out character by character, so base64 for anything beyond
a small file is unreliable. Instead create_upload_link returns a single-use URL: Claude's code
environment POSTs the file there (curl), or the user opens it in a browser and drops the file.
The upload id then goes into a purchase invoice as attachment_upload_id.

Links live in this process only: they expire after an hour and are gone on restart; uploaded
files are kept for a day. The path is not under /mcp, so the Cloudflare rule that limits /mcp to
Anthropic's addresses does not apply; the 256-bit single-use token is the protection.
"""

import html
import json
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

LINK_TTL = 3600
FILE_TTL = 24 * 3600
MAX_BYTES = 25 * 1024 * 1024  # larger than any invoice, statement or e-invoice the tools take
UPLOAD_DIR = Path(tempfile.gettempdir()) / "merit-mcp-uploads"


class TooLarge(Exception):
    pass


@dataclass
class Slot:
    upload_id: str
    expires: float
    used: bool = False


@dataclass
class Uploads:
    public_url: str
    slots: dict[str, Slot] = field(default_factory=dict)  # token -> slot
    files: dict[str, tuple[Path, str, str, float]] = field(default_factory=dict)  # id -> (path, name, mime, at)

    def _sweep(self) -> None:
        now = time.time()
        self.slots = {t: s for t, s in self.slots.items() if s.expires > now and not s.used}
        for uid, (path, *_rest, at) in list(self.files.items()):
            if at + FILE_TTL < now:
                path.unlink(missing_ok=True)
                del self.files[uid]

    def create_link(self) -> dict:
        self._sweep()
        token, upload_id = secrets.token_urlsafe(32), "up_" + secrets.token_hex(8)
        self.slots[token] = Slot(upload_id, time.time() + LINK_TTL)
        url = f"{self.public_url}/upload/{token}"
        return {
            "upload_id": upload_id,
            "upload_url": url,
            "expires_in_minutes": LINK_TTL // 60,
            "how": (
                f'From your code environment: curl -sS -F "file=@/path/to/file.pdf" {url}  '
                "(single use). If your environment cannot reach it, give the user this URL to open in "
                "a browser and drop the file there. Then pass this upload id to the tool that asked for the "
                "file (attachment_upload_id or upload_id): \"" + upload_id + "\"."
            ),
        }

    def get(self, upload_id: str) -> tuple[str, bytes, str]:
        self._sweep()
        if upload_id not in self.files:
            pending = any(s.upload_id == upload_id for s in self.slots.values())
            raise ToolError(f"upload {upload_id} has not arrived yet" if pending else f"no upload {upload_id} (expired or never made)")
        path, name, mime, _ = self.files[upload_id]
        return name, path.read_bytes(), mime

    async def _store(self, slot: Slot, name: str, mime: str, chunks) -> dict:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / slot.upload_id
        size = 0
        with path.open("wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_BYTES:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise TooLarge()
                f.write(chunk)
        slot.used = True
        self.files[slot.upload_id] = (path, name, mime, time.time())
        return {"upload_id": slot.upload_id, "file_name": name, "size": size}

    async def endpoint(self, request: Request) -> Response:
        token = request.path_params["token"]
        slot = self.slots.get(token)
        if slot is None or slot.used or slot.expires < time.time():
            return JSONResponse({"error": "upload link is invalid, used or expired; ask for a new one"}, status_code=404)
        if request.method == "GET":
            return HTMLResponse(PAGE.format(upload_id=html.escape(slot.upload_id)))
        try:
            return await self._receive(request, slot)
        except TooLarge:
            return JSONResponse({"error": f"file is over {MAX_BYTES // 1024 // 1024} MB"}, status_code=413)

    async def _receive(self, request: Request, slot: Slot) -> Response:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            form = await request.form(max_part_size=MAX_BYTES)
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                return JSONResponse({"error": 'send the file in a form field named "file"'}, status_code=400)

            async def parts():
                while chunk := await upload.read(1 << 20):
                    yield chunk

            result = await self._store(slot, upload.filename or "file", upload.content_type or "", parts())
            await form.close()
        else:  # raw body: curl --data-binary @file "URL?name=offer.pdf"
            name = request.query_params.get("name") or "file"
            result = await self._store(slot, name, ctype if ctype and "form" not in ctype else "", request.stream())
        if "text/html" in request.headers.get("accept", ""):
            return HTMLResponse(DONE.format(info=html.escape(json.dumps(result))))
        return JSONResponse(result)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Upload a file</title><style>body{{font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem}}
button{{font:inherit;padding:.6rem 1.2rem}}</style></head><body>
<h1>Upload a file for Claude</h1><p>Single use. After uploading, tell Claude it is done
(upload id <code>{upload_id}</code>).</p>
<form method="post" enctype="multipart/form-data"><p><input type="file" name="file" required></p>
<p><button>Upload</button></p></form></body></html>"""

DONE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Uploaded</title><style>body{{font:16px system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem}}</style>
</head><body><h1>Uploaded</h1><p>Tell Claude the file is there.</p><pre>{info}</pre></body></html>"""
