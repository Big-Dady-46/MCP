"""NexusAI remote MCP gateway — Streamable HTTP transport for AI hosts
(ChatGPT connectors, curl, OpenCode-over-HTTP, OpenAI Agents API, etc.).

Design (all verified by tests + gateway_integration_check.py):

- Transport: MCP Streamable HTTP (stateless mode — safe for serverless,
  no session affinity required).
- Auth (dual):
  1. Static bearer token (Authorization: Bearer <MCP_GATEWAY_TOKEN>) for
     clients that can send custom headers (curl, agents, scripts).
  2. Minimal stateless OAuth 2.1 (PKCE S256 + DCR + HMAC-signed codes and
     tokens, single-user login page) for ChatGPT, which does NOT support
     static custom auth headers — only OAuth or no-auth.
  Tokens/codes are HMAC-signed with a key derived from MCP_GATEWAY_TOKEN;
  nothing is stored server-side (serverless-safe).
- Tools: READ-ONLY subset of the store MCP tools (13 of 25). Write tools are
  intentionally NOT exposed: on Vercel the database lives in /tmp (ephemeral,
  empty at every cold start), so write access would only create false
  confidence. A [DEMO] dataset is seeded lazily so reads return data.
- Fail-closed: without MCP_GATEWAY_TOKEN the gateway serves 503/healthz
  "disabled" and never exposes tools.

Environment variables:
  MCP_GATEWAY_TOKEN   (required) long random secret; also the login password
  MCP_GATEWAY_USER    (optional, default "admin") login username
  GATEWAY_MODE        (optional, default "readonly-demo"; only supported mode)

This module never touches the real local nexusai.db / store.db / notes.db:
it only uses store_db, whose path is /tmp on Vercel and env-overridable in
tests (temporary databases only).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import store_db as sdb
import store_server
import oauth_meta
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_TOKEN = os.environ.get("MCP_GATEWAY_TOKEN", "").strip()
GATEWAY_USER = os.environ.get("MCP_GATEWAY_USER", "admin").strip() or "admin"
GATEWAY_MODE = os.environ.get("GATEWAY_MODE", "readonly-demo").strip() or "readonly-demo"

ACCESS_TTL = int(os.environ.get("MCP_GATEWAY_ACCESS_TTL", str(24 * 3600)))
REFRESH_TTL = int(os.environ.get("MCP_GATEWAY_REFRESH_TTL", str(30 * 24 * 3600)))
CODE_TTL = 60

# Key derivation: never use the raw token as HMAC key material directly.
_HMAC_KEY = (
    hmac.new(GATEWAY_TOKEN.encode(), b"nexusai-gateway-v1", hashlib.sha256).digest()
    if GATEWAY_TOKEN
    else b"\x00"
)

TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
GRANT_TYPES = ["authorization_code", "refresh_token"]
CODE_CHALLENGE_METHODS = ["S256"]
SCOPES = ["mcp:read"]

READ_ONLY_TOOLS = [d["name"] for d in store_server.get_tool_definitions() if d.get("read_only")]

_gateway_db_ready = False


# ---------------------------------------------------------------------------
# Fail-closed mode
# ---------------------------------------------------------------------------

def gateway_enabled() -> bool:
    return bool(GATEWAY_TOKEN) and GATEWAY_MODE == "readonly-demo"


# ---------------------------------------------------------------------------
# Stateless HMAC tokens:  ng1.<b64url(payload)>.<b64url(hmac)>
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(_HMAC_KEY, body.encode(), hashlib.sha256).digest()
    return f"ng1.{body}.{_b64e(sig)}"


def _verify_token(token: str, expected_typ: str) -> dict | None:
    """Verify HMAC + structure + expiry. Returns payload or None."""
    try:
        ver, body, sig = token.split(".")
        if ver != "ng1":
            return None
        want = _b64e(hmac.new(_HMAC_KEY, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, sig):
            return None
        payload = json.loads(_b64d(body))
        if payload.get("typ") != expected_typ:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _issue_access() -> str:
    now = int(time.time())
    return _sign({"typ": "access", "iat": now, "exp": now + ACCESS_TTL, "scope": "mcp:read"})


def _issue_refresh() -> str:
    now = int(time.time())
    return _sign({"typ": "refresh", "iat": now, "exp": now + REFRESH_TTL, "scope": "mcp:read"})


def _issue_code(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    now = int(time.time())
    return _sign({
        "typ": "code", "iat": now, "exp": now + CODE_TTL,
        "client_id": client_id, "redirect_uri": redirect_uri,
        "challenge": challenge, "state": state,
    })


def _issue_client_id(redirect_uris: list) -> str:
    now = int(time.time())
    return _sign({
        "typ": "client", "iat": now, "exp": now + 10 * 365 * 24 * 3600,
        "redirect_uris": list(redirect_uris),
    })


def _client_redirect_uris(client_id: str) -> list | None:
    payload = _verify_token(client_id, "client") if client_id.startswith("ng1.") else None
    if not payload:
        return None
    uris = payload.get("redirect_uris")
    return uris if isinstance(uris, list) and uris else None


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    return hmac.compare_digest(_b64e(digest), challenge)


# ---------------------------------------------------------------------------
# Gateway DB readiness (lazy — never at import time)
# ---------------------------------------------------------------------------

def _ensure_gateway_db() -> None:
    """Create schema + idempotent [DEMO] seed once per process.

    Uses store_db only. On Vercel that is /tmp/store.db (ephemeral); in
    tests it is a temp path set via NEXUSAI_STORE_DB. Never the real local
    store.db unless someone deliberately points NEXUSAI_STORE_DB at it
    (the integration check hashes the real DBs and forbids changes).
    """
    global _gateway_db_ready
    if _gateway_db_ready:
        return
    sdb.init_db()
    sdb.seed_demo()
    _gateway_db_ready = True


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _valid_bearer(token: str) -> bool:
    if not token or not GATEWAY_TOKEN:
        return False
    if hmac.compare_digest(token, GATEWAY_TOKEN):
        return True
    return _verify_token(token, "access") is not None


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme or "https")
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}".rstrip("/")


# ---------------------------------------------------------------------------
# MCP server (read-only tool subset) + streamable HTTP app
# ---------------------------------------------------------------------------

def _build_mcp_server() -> MCPServer:
    gw = MCPServer(
        "nexusai-store-gateway",
        title="NexusAI Store (read-only demo gateway)",
        instructions=(
            "READ-ONLY remote view of the NexusAI beverage store. Only "
            "list/get/summary/report tools are available; write tools "
            "(sales, purchases, stock edits, expenses) are intentionally "
            "not exposed because this gateway runs on serverless ephemeral "
            "storage. Amounts are Pakistani Rupees (PKR). Data is a [DEMO] "
            "dataset that resets on cold starts."
        ),
        version="1.0.0",
    )
    for name in READ_ONLY_TOOLS:
        gw.tool()(getattr(store_server, name))
    return gw


_mcp_server = _build_mcp_server()
# DNS-rebinding protection is disabled deliberately: on Vercel the Host is
# the deployment domain (not localhost), and every /mcp request already
# requires bearer auth, which is the real protection here.
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

_mcp_inner_app = _mcp_server.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class _ForceMcpPath:
    """ASGI wrapper: Vercel rewrites /mcp_gateway[/...] → function; force the
    inner streamable-HTTP app to always see its canonical /mcp path."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["path"] = "/mcp"
        scope["raw_path"] = b"/mcp"
        await self.app(scope, receive, send)


_mcp_host = _ForceMcpPath(_mcp_inner_app)

# The streamable-HTTP session manager must have its lifespan (task group)
# entered before any request is served. Some hosts (uvicorn) run ASGI
# lifespan events; serverless platforms may not. So we self-start lazily
# on the first request and keep it running for the process lifetime.
_mcp_started = False
_mcp_start_lock: asyncio.Lock | None = None
# Strong reference: if this async context manager is garbage-collected it
# shuts the session manager down, so it must live for the process lifetime.
_mcp_lifespan_cm = None


def _get_start_lock() -> asyncio.Lock:
    global _mcp_start_lock
    if _mcp_start_lock is None:
        _mcp_start_lock = asyncio.Lock()
    return _mcp_start_lock


async def _ensure_mcp_started() -> None:
    global _mcp_started, _mcp_lifespan_cm
    if _mcp_started:
        return
    async with _get_start_lock():
        if _mcp_started:
            return
        cm = _mcp_inner_app.router.lifespan_context(_mcp_inner_app)
        await cm.__aenter__()
        _mcp_lifespan_cm = cm
        _mcp_started = True


async def _raw_json(send, status: int, payload: dict, extra_headers: dict | None = None) -> None:
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    for k, v in (extra_headers or {}).items():
        headers.append((k.lower().encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _McpWithAuth:
    """ASGI wrapper in front of the streamable-HTTP app.

    Fail-closed (503 when token missing / mode unsupported), then requires
    Authorization: Bearer <static token or valid OAuth access token>.
    On success, ensures the (demo) gateway DB exists and forwards the
    request untouched to the MCP transport.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        if not gateway_enabled():
            await _raw_json(send, 503, {
                "status": "disabled",
                "reason": "MCP_GATEWAY_TOKEN missing or GATEWAY_MODE unsupported",
            })
            return
        hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        authz = hdrs.get("authorization", "")
        token = authz[7:].strip() if authz.lower().startswith("bearer ") else ""
        if not _valid_bearer(token):
            base = _base_url_from_scope(scope)
            await _raw_json(
                send, 401,
                {"error": "unauthorized", "error_description": "Bearer token required."},
                {"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'},
            )
            return
        try:
            _ensure_gateway_db()
            await _ensure_mcp_started()
        except Exception:
            await _raw_json(send, 503, {"error": "database_unavailable"})
            return
        await self.inner(scope, receive, send)


def _base_url_from_scope(scope) -> str:
    hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    proto = hdrs.get("x-forwarded-proto", scope.get("scheme", "https"))
    host = hdrs.get("x-forwarded-host") or hdrs.get("host") or scope.get("server", ["localhost", "443"])
    if isinstance(host, (list, tuple)):
        host = f"{host[0]}:{host[1]}" if len(host) > 1 else str(host[0])
    return f"{proto}://{host}".rstrip("/")


# ---------------------------------------------------------------------------
# OAuth 2.1 (minimal, stateless) — for ChatGPT connectors
# ---------------------------------------------------------------------------

_LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>NexusAI Gateway Login</title>
<style>
 body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
 align-items:center;justify-content:center;min-height:100vh;margin:0}}
 form{{background:#1e293b;padding:2rem;border-radius:12px;min-width:280px}}
 h1{{font-size:1.1rem;margin:0 0 .3rem}} p{{color:#94a3b8;font-size:.85rem;margin:0 0 1rem}}
 label{{display:block;font-size:.8rem;margin:.7rem 0 .2rem;color:#94a3b8}}
 input{{width:100%;padding:.5rem;border-radius:6px;border:1px solid #334155;
 background:#0f172a;color:#e2e8f0;box-sizing:border-box}}
 button{{margin-top:1.2rem;width:100%;padding:.6rem;border:0;border-radius:6px;
 background:#2563eb;color:#fff;font-weight:600;cursor:pointer}}
</style></head><body>
<form method="post" action="/authorize">
 <h1>NexusAI Store Gateway</h1>
 <p>Sign in to approve this AI assistant (read-only demo access).</p>
 <input type="hidden" name="req" value="{req}">
 <label>Username</label><input name="username" autocomplete="username" required>
 <label>Password (gateway token)</label>
 <input name="password" type="password" autocomplete="current-password" required>
 <button type="submit">Approve &amp; continue</button>
</form></body></html>"""


async def _oauth_pr_metadata(request: Request) -> Response:
    return JSONResponse(oauth_meta.protected_resource_metadata(_base_url(request)))


async def _oauth_as_metadata(request: Request) -> Response:
    return JSONResponse(oauth_meta.authorization_server_metadata(_base_url(request)))


async def _oauth_register(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    uris = body.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not uris
        or not all(isinstance(u, str) and u.startswith(("https://", "http://localhost")) for u in uris)
    ):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    now = int(time.time())
    return JSONResponse({
        "client_id": _issue_client_id(uris),
        "client_id_issued_at": now,
        "client_name": str(body.get("client_name", "MCP client"))[:120],
        "redirect_uris": uris,
        "token_endpoint_auth_method": "none",
        "grant_types": GRANT_TYPES,
        "response_types": ["code"],
    }, status_code=201)


def _valid_authorize_params(qs) -> tuple | None:
    """Validate authorize query params (starlette QueryParams .get -> str)."""
    if not gateway_enabled():
        return None
    if qs.get("response_type", "") != "code":
        return None
    client_id = qs.get("client_id", "")
    redirect_uri = qs.get("redirect_uri", "")
    challenge = qs.get("code_challenge", "")
    method = qs.get("code_challenge_method", "")
    state = qs.get("state", "")
    uris = _client_redirect_uris(client_id)
    if uris is None or redirect_uri not in uris or method != "S256" or len(challenge) < 40:
        return None
    return client_id, redirect_uri, challenge, state, ""


async def _oauth_authorize_get(request: Request) -> Response:
    parsed = _valid_authorize_params(request.query_params)
    if parsed is None:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    client_id, redirect_uri, challenge, state, _ = parsed
    req_blob = _sign({
        "typ": "areq", "iat": int(time.time()), "exp": int(time.time()) + CODE_TTL * 10,
        "client_id": client_id, "redirect_uri": redirect_uri,
        "challenge": challenge, "state": state,
    })
    html = _LOGIN_PAGE.format(req=req_blob.replace('"', "&quot;"))
    return HTMLResponse(html)


async def _oauth_authorize_post(request: Request) -> Response:
    form = dict(await request.form())
    req_blob = str(form.get("req", ""))
    areq = _verify_token(req_blob, "areq") if req_blob.startswith("ng1.") else None
    if areq is None or not gateway_enabled():
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    user_ok = hmac.compare_digest(str(form.get("username", "")), GATEWAY_USER)
    pass_ok = hmac.compare_digest(str(form.get("password", "")), GATEWAY_TOKEN)
    if not (user_ok and pass_ok):
        return HTMLResponse(
            "<p style='font-family:sans-serif'>Invalid credentials. "
            "<a href='javascript:history.back()'>Try again</a></p>",
            status_code=401,
        )
    code = _issue_code(
        areq["client_id"], areq["redirect_uri"], areq["challenge"], areq.get("state", "")
    )
    sep = "&" if "?" in areq["redirect_uri"] else "?"
    target = (
        f"{areq['redirect_uri']}{sep}"
        + urlencode({"code": code, "state": areq.get("state", "")})
    )
    return RedirectResponse(target, status_code=302)


async def _oauth_token(request: Request) -> Response:
    form = dict(await request.form())
    grant = form.get("grant_type", "")
    if grant == "authorization_code":
        payload = _verify_token(str(form.get("code", "")), "code")
        if payload is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if str(form.get("redirect_uri", "")) != payload.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if str(form.get("client_id", "")) != payload.get("client_id"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _pkce_ok(str(form.get("code_verifier", "")), payload.get("challenge", "")):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        _ensure_gateway_db()
        return JSONResponse({
            "access_token": _issue_access(),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": _issue_refresh(),
            "scope": "mcp:read",
        })
    if grant == "refresh_token":
        payload = _verify_token(str(form.get("refresh_token", "")), "refresh")
        if payload is None:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        _ensure_gateway_db()
        return JSONResponse({
            "access_token": _issue_access(),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "refresh_token": _issue_refresh(),
            "scope": "mcp:read",
        })
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def _healthz(request: Request) -> Response:
    db_ok = False
    if gateway_enabled():
        try:
            _ensure_gateway_db()
            db_ok = True
        except Exception:
            db_ok = False
    healthy = gateway_enabled() and db_ok
    return JSONResponse(
        {
            "status": "ok" if healthy else ("degraded" if gateway_enabled() else "disabled"),
            "mode": GATEWAY_MODE,
            "transport": "streamable-http",
            "tools_total": len(READ_ONLY_TOOLS),
            "auth": ["bearer" if GATEWAY_TOKEN else None, "oauth2-pkce"],
            "database": {"reachable": db_ok, "persistent": False},
        },
        status_code=200 if healthy else 503,
    )


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _outer_lifespan(_app):
    # Start the MCP session manager early when the host supports lifespan;
    # _ensure_mcp_started() also self-starts lazily for hosts that don't.
    if gateway_enabled():
        try:
            await _ensure_mcp_started()
        except Exception:
            pass
    yield


_router = Starlette(
    debug=False,
    lifespan=_outer_lifespan,
    routes=[
        Route("/healthz", _healthz, methods=["GET"]),
        # OAuth 2.1 (spec paths + /mcp_gateway/* rewrite targets)
        Route("/.well-known/oauth-protected-resource", _oauth_pr_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/{rest:path}", _oauth_pr_metadata, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _oauth_as_metadata, methods=["GET"]),
        Route("/mcp_gateway/pr", _oauth_pr_metadata, methods=["GET"]),
        Route("/mcp_gateway/as", _oauth_as_metadata, methods=["GET"]),
        Route("/mcp_gateway/register", _oauth_register, methods=["POST"]),
        Route("/mcp_gateway/authorize", _oauth_authorize_get, methods=["GET"]),
        Route("/mcp_gateway/authorize", _oauth_authorize_post, methods=["POST"]),
        Route("/mcp_gateway/token", _oauth_token, methods=["POST"]),
        # Direct paths too (useful locally / behind proxies that don't rewrite)
        Route("/register", _oauth_register, methods=["POST"]),
        Route("/authorize", _oauth_authorize_get, methods=["GET"]),
        Route("/authorize", _oauth_authorize_post, methods=["POST"]),
        Route("/token", _oauth_token, methods=["POST"]),
    ],
)

_mcp_asgi = _McpWithAuth(_mcp_host)


class _GatewayApp:
    """Top-level ASGI app.

    MCP paths are dispatched directly (no Starlette Mount: Mount issues a
    307 slash-redirect that breaks POST clients). Everything else goes to
    the router (healthz, OAuth endpoints, lifespan).
    """

    def __init__(self):
        self.router = _router

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") in ("/mcp", "/mcp_gateway"):
            scoped = dict(scope)
            scoped["path"] = "/mcp"
            scoped["raw_path"] = b"/mcp"
            await _mcp_asgi(scoped, receive, send)
            return
        await self.router(scope, receive, send)


app = _GatewayApp()
