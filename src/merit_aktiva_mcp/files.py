"""Files for the tools that take one (a vendor's PDF, a bank statement, an e-invoice XML).

Local mode (stdio, on the user's own computer): the tool gets file_path, a path on this computer.
Server mode: a remote server cannot read the user's disk, so the file comes through a single-use
upload link (create_upload_link, uploads.py) and the tool gets its upload id.
"""

from pathlib import Path
from typing import Protocol

from fastmcp.exceptions import ToolError

MAX_BYTES = 25 * 1024 * 1024


class Files(Protocol):
    def get(self, ref: str | None, path: str | None, kinds: tuple[str, ...]) -> tuple[str, bytes]:
        """(file name, contents) for an upload id (ref) or a local path, of one of the suffixes in kinds."""


class LocalFiles:
    def get(self, ref: str | None, path: str | None, kinds: tuple[str, ...]) -> tuple[str, bytes]:
        if ref and not path:
            raise ToolError("upload links exist only on a server; give file_path, the file's path on this computer")
        if not path:
            raise ToolError("give file_path, the file's path on this computer")
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"no file at {path}")
        if p.suffix.lower() not in kinds:
            raise ToolError(f"{p.name} must be a {' or '.join(kinds)} file")
        size = p.stat().st_size
        if size > MAX_BYTES:
            raise ToolError(f"{p.name} is {size:,} bytes, over the {MAX_BYTES // 1024 // 1024} MB limit")
        return p.name, p.read_bytes()


class ServerFiles:
    def __init__(self, uploads):
        self.uploads = uploads

    def get(self, ref: str | None, path: str | None, kinds: tuple[str, ...]) -> tuple[str, bytes]:
        if path and not ref:
            raise ToolError("this server cannot read your disk; call create_upload_link, upload the file there, "
                            "and pass its upload id")
        if not ref:
            raise ToolError("call create_upload_link, upload the file there, and pass its upload id")
        name, data, _mime = self.uploads.get(ref)
        return name, data
