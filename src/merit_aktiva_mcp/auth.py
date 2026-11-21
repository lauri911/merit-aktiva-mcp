"""Sign-in for server (http) mode: Microsoft Entra ID or Google, limited to ALLOWED_EMAILS.

The provider is only the sign-in: the server calls no Microsoft or Google API with the token. The
allow-list is checked on every request, so a misconfigured app registration can't open the books
to everyone with a Microsoft or Google account.
"""

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.azure import AzureProvider
from fastmcp.server.auth.providers.google import GoogleProvider

from .config import Config

log = logging.getLogger(__name__)

# claude.ai (web, desktop and mobile) uses these callbacks to connect a custom connector.
CLIENT_REDIRECTS = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]


def _email(claims: dict) -> str:
    return (claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "").lower()


class _AllowList:
    """Mixed into a provider: reject tokens of anyone not in the allow-list."""

    _allowed_emails: frozenset[str] = frozenset()

    def _allowed(self, claims: dict) -> bool:
        return _email(claims) in self._allowed_emails

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await super().load_access_token(token)  # type: ignore[misc]
        if access is None:
            return None
        claims = access.claims or {}
        if not self._allowed(claims):
            log.warning("rejected sign-in by %r: not in ALLOWED_EMAILS", _email(claims))
            return None
        return access


class AllowedEntra(_AllowList, AzureProvider):
    def __init__(self, *, allowed_emails: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_emails = allowed_emails


class AllowedGoogle(_AllowList, GoogleProvider):
    def __init__(self, *, allowed_emails: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_emails = allowed_emails

    def _allowed(self, claims: dict) -> bool:
        verified = claims.get("email_verified")
        return super()._allowed(claims) and (verified is True or str(verified).lower() == "true")


REQUIRED = ("PUBLIC_URL", "AUTH_PROVIDER", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "ALLOWED_EMAILS", "JWT_SIGNING_KEY")


def build_auth(cfg: Config):
    """The sign-in provider for server mode; refuses to start without complete settings."""
    have = {"PUBLIC_URL": cfg.public_url, "AUTH_PROVIDER": cfg.auth_provider, "OAUTH_CLIENT_ID": cfg.client_id,
            "OAUTH_CLIENT_SECRET": cfg.client_secret, "ALLOWED_EMAILS": cfg.allowed_emails,
            "JWT_SIGNING_KEY": cfg.jwt_signing_key, "ENTRA_TENANT_ID": cfg.tenant_id}
    needed = list(REQUIRED) + (["ENTRA_TENANT_ID"] if cfg.auth_provider == "entra" else [])
    missing = [n for n in needed if not have[n]]
    if missing:
        raise SystemExit("Server mode (--transport http) needs sign-in settings; missing: " + ", ".join(missing)
                         + ". See docs/server-setup.md.")
    common = dict(allowed_emails=cfg.allowed_emails, client_id=cfg.client_id, client_secret=cfg.client_secret,
                  base_url=cfg.public_url, jwt_signing_key=cfg.jwt_signing_key,
                  allowed_client_redirect_uris=CLIENT_REDIRECTS)
    if cfg.auth_provider == "entra":
        return AllowedEntra(tenant_id=cfg.tenant_id, required_scopes=["mcp.access"],
                            additional_authorize_scopes=["User.Read"], **common)
    if cfg.auth_provider == "google":
        return AllowedGoogle(required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"], **common)
    raise SystemExit(f"AUTH_PROVIDER must be entra or google, not {cfg.auth_provider!r}")
