"""Server mode sign-in: Entra or Google, complete settings or no start, and only allowed emails."""

import asyncio
import dataclasses

import pytest
from fastmcp.server.auth import AccessToken

from merit_aktiva_mcp import auth, config
from merit_aktiva_mcp.main import build

BASE = config.Config(api_id="id", api_key="key", base_url=config.DEFAULT_BASE_URL, allow_write=False,
                     extra_instructions="", company_name=None, transport="http", public_url="https://merit.example.com",
                     auth_provider="google", client_id="cid", client_secret="secret", tenant_id=None,
                     allowed_emails=frozenset({"anna@example.com"}), jwt_signing_key="k" * 32)


def test_http_without_settings_refuses_to_start():
    cfg = dataclasses.replace(BASE, client_secret=None, allowed_emails=frozenset())
    with pytest.raises(SystemExit, match="OAUTH_CLIENT_SECRET, ALLOWED_EMAILS"):
        build(cfg)
    with pytest.raises(SystemExit, match="ENTRA_TENANT_ID"):
        auth.build_auth(dataclasses.replace(BASE, auth_provider="entra"))
    with pytest.raises(SystemExit, match="entra or google"):
        auth.build_auth(dataclasses.replace(BASE, auth_provider="github"))


def test_provider_per_setting():
    assert isinstance(auth.build_auth(BASE), auth.AllowedGoogle)
    entra = auth.build_auth(dataclasses.replace(BASE, auth_provider="entra", tenant_id="tenant-guid"))
    assert isinstance(entra, auth.AllowedEntra)


def token(claims):
    return AccessToken(token="t", client_id="c", scopes=[], claims=claims)


@pytest.mark.parametrize("claims,allowed", [
    ({"email": "Anna@Example.com", "email_verified": True}, True),
    ({"email": "anna@example.com", "email_verified": "true"}, True),
    ({"email": "anna@example.com", "email_verified": False}, False),
    ({"email": "eve@example.com", "email_verified": True}, False),
])
def test_google_allow_list(monkeypatch, claims, allowed):
    async def parent(self, t):
        return token(claims)
    monkeypatch.setattr(auth.GoogleProvider, "load_access_token", parent)
    result = asyncio.run(auth.build_auth(BASE).load_access_token("t"))
    assert (result is not None) is allowed


def test_entra_allow_list(monkeypatch):
    async def parent(self, t):
        return token({"preferred_username": "eve@example.com"})
    monkeypatch.setattr(auth.AzureProvider, "load_access_token", parent)
    entra = auth.build_auth(dataclasses.replace(BASE, auth_provider="entra", tenant_id="tenant-guid"))
    assert asyncio.run(entra.load_access_token("t")) is None
