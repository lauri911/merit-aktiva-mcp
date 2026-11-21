"""Settings, all from environment variables. Any of them may instead be given as a file whose path
is in NAME_FILE (Docker secrets, for example): MERIT_API_KEY_FILE=/run/secrets/merit_api_key."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://aktiva.merit.ee/api"  # Estonian Merit Aktiva


def setting(name: str, default: str | None = None) -> str | None:
    """NAME from the environment, else the contents of the file named by NAME_FILE, else default."""
    value = os.environ.get(name, "").strip()
    # A Claude Desktop extension passes an optional field left empty as its ${user_config.x} placeholder.
    if value and not re.fullmatch(r"\$\{[^}]*\}", value):
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            text = Path(path).expanduser().read_text().strip()
        except OSError as e:
            raise SystemExit(f"{name}_FILE={path}: cannot read it ({e.strerror or e})") from None
        if text:
            return text
    return default


def _flag(name: str) -> bool:
    return (setting(name) or "").lower() in {"true", "1", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_id: str | None
    api_key: str | None
    base_url: str
    allow_write: bool
    extra_instructions: str
    transport: Literal["stdio", "http"]
    # Server (http) mode only
    public_url: str | None
    auth_provider: str | None
    client_id: str | None
    client_secret: str | None
    tenant_id: str | None
    allowed_emails: frozenset[str]
    jwt_signing_key: str | None

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url or "").netloc


def load(transport: str | None = None) -> Config:
    chosen = (transport or setting("MCP_TRANSPORT") or "stdio").lower()
    if chosen not in ("stdio", "http"):
        raise SystemExit(f"MCP_TRANSPORT must be stdio or http, not {chosen!r}")
    return Config(
        api_id=setting("MERIT_API_ID"),
        api_key=setting("MERIT_API_KEY"),
        base_url=(setting("MERIT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        allow_write=_flag("MERIT_ALLOW_WRITE"),
        extra_instructions=setting("MERIT_EXTRA_INSTRUCTIONS") or "",
        transport=chosen,  # type: ignore[arg-type]
        public_url=(setting("PUBLIC_URL") or "").rstrip("/") or None,
        auth_provider=(setting("AUTH_PROVIDER") or "").lower() or None,
        client_id=setting("OAUTH_CLIENT_ID"),
        client_secret=setting("OAUTH_CLIENT_SECRET"),
        tenant_id=setting("ENTRA_TENANT_ID"),
        allowed_emails=frozenset(e.strip().lower() for e in (setting("ALLOWED_EMAILS") or "").split(",") if e.strip()),
        jwt_signing_key=setting("JWT_SIGNING_KEY"),
    )
