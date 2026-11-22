# Server setup (claude.ai, teams, phone)

In server mode the MCP server runs on a machine you control, reachable over HTTPS, and people add it
in claude.ai as a custom connector. Everyone signs in with **Microsoft Entra ID** or **Google**; only
the email addresses in `ALLOWED_EMAILS` get in.

You need: a machine with Docker, a public HTTPS address for it (for example `https://merit.example.com`,
through a Cloudflare Tunnel or a reverse proxy), and an app registration with Microsoft or Google.

## 1. Sign-in app

The server's callback address is `<PUBLIC_URL>/auth/callback`, for example
`https://merit.example.com/auth/callback`.

### Microsoft Entra ID (Microsoft 365)

1. Azure portal → **Microsoft Entra ID → App registrations → New registration**. Name it, choose
   "Accounts in this organizational directory only", and add a **Web** redirect URI
   `<PUBLIC_URL>/auth/callback`.
2. **Expose an API**: set the Application ID URI (the default `api://<client id>` is fine) and add a
   scope named `mcp.access` (admins and users may consent).
3. **Certificates & secrets → New client secret**: copy the value.
4. Note the **Application (client) ID** and the **Directory (tenant) ID**.
5. Optional but recommended: Enterprise applications → your app → Properties → **Assignment
   required = Yes**, then assign only the people who should use it.

Settings: `AUTH_PROVIDER=entra`, `OAUTH_CLIENT_ID=<client id>`, `ENTRA_TENANT_ID=<tenant id>`, and the
secret as `oauth_client_secret`.

### Google

1. Google Cloud console → **APIs & Services → OAuth consent screen**: set it up (Internal if you use
   Google Workspace and everyone is in it; otherwise External, and add your users as test users or
   publish the app).
2. **Credentials → Create credentials → OAuth client ID → Web application**; add the authorized
   redirect URI `<PUBLIC_URL>/auth/callback`.
3. Copy the client ID and client secret.

Settings: `AUTH_PROVIDER=google`, `OAUTH_CLIENT_ID=<client id>`, and the secret as `oauth_client_secret`.
Google accounts must have a verified email to get in.

## 2. Settings and secrets

```bash
git clone https://github.com/lauri911/merit-aktiva-mcp && cd merit-aktiva-mcp
cp deploy/.env.example deploy/.env          # PUBLIC_URL, AUTH_PROVIDER, OAUTH_CLIENT_ID, ALLOWED_EMAILS ...
mkdir -p deploy/secrets && chmod 700 deploy/secrets
printf '%s' 'your Merit API ID'  > deploy/secrets/merit_api_id
printf '%s' 'your Merit API key' > deploy/secrets/merit_api_key
printf '%s' 'the OAuth client secret' > deploy/secrets/oauth_client_secret
openssl rand -hex 32 > deploy/secrets/jwt_signing_key   # signs the server's own tokens; keep it
touch deploy/secrets/cloudflared_token                  # fill it only if you use the tunnel
chmod 600 deploy/secrets/*
```

All server settings:

| Variable | Meaning |
|---|---|
| `PUBLIC_URL` | The HTTPS address of this server, without `/mcp` |
| `AUTH_PROVIDER` | `entra` or `google` |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | From the app registration |
| `ENTRA_TENANT_ID` | Entra only |
| `ALLOWED_EMAILS` | Comma-separated; nobody else gets in |
| `JWT_SIGNING_KEY` | A long random string; the server signs its own tokens with it |
| `MERIT_API_ID`, `MERIT_API_KEY`, `MERIT_ALLOW_WRITE`, `MERIT_COMPANY_NAME`, `MERIT_EXTRA_INSTRUCTIONS` | As in the README |

Any of them can be given as a file with `NAME_FILE=/path`; `deploy/compose.yaml` does that for the secrets.
The server refuses to start in http mode until all sign-in settings are there.

## 3. Start

With your own HTTPS reverse proxy (pointing at `127.0.0.1:8000`):

```bash
docker compose -f deploy/compose.yaml up -d --build
```

With a Cloudflare Tunnel instead (no open ports): create a tunnel in Cloudflare Zero Trust, put its
token in `deploy/secrets/cloudflared_token`, give it a public hostname that points to
`http://merit-mcp:8000`, then:

```bash
docker compose -f deploy/compose.yaml --profile tunnel up -d --build
```

Check: `curl https://merit.example.com/healthz` answers `ok`.

## 4. Connect claude.ai

claude.ai → **Settings → Connectors → Add custom connector**: name "Merit", URL `<PUBLIC_URL>/mcp`.
Each person connects and signs in. After you add or remove tools (an update, or switching
`MERIT_ALLOW_WRITE`), people need to disconnect and reconnect: claude.ai caches the tool list.

## Files in server mode

A server can't read anyone's disk, so tools that take a file (a vendor's PDF, a CAMT.053 statement,
an e-invoice XML) get it through a single-use upload link: Claude calls `create_upload_link`, uploads
the file there (or gives you the link to open), and passes the upload id. Links last an hour; files a
day; at most 25 MB.

## Updating

```bash
git pull && docker compose -f deploy/compose.yaml up -d --build
```
