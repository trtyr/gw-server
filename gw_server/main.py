"""gw-server — Google Workspace gateway.

Endpoints:
  GET  /healthz   (no auth)      liveness probe
  GET  /accounts  (Bearer)       list known accounts (no token values)
  POST /exec      (Bearer)       authenticated Google REST API proxy

Auth is a single shared bearer token (GW_TOKEN). Google-side errors are
reported in the envelope (ok:false + status) with HTTP 200, mirroring the
workspace.js CLI behaviour; only gateway auth failures produce HTTP 401.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import config
from .login import DEFAULT_CLIENT_ID, DEFAULT_SCOPES
from .mcp_server import mcp, tokens
from .tokens import (
    SERVICE_ROUTES,
    TokenError,
    TokenNotFound,
    build_url,
    normalize_email,
)


def serve() -> None:
    """Console-script entrypoint: validate env, then run uvicorn."""
    import uvicorn

    uvicorn.run(
        "gw_server.main:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("gw.server")

# MCP app + lifespan bridge: the session manager's task group must be running
# before any /mcp request; Starlette does not run lifespans of mounted sub-apps,
# so we re-enter mcp_app's own lifespan from FastAPI's. Using mcp_app's own
# lifespan (instead of the lowlevel server's _session_manager) guarantees the
# manager being run is exactly the one this app instance serves with.
mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True)
_mcp_lifespan = mcp_app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app: FastAPI):
    async with _mcp_lifespan(app):
        yield


app = FastAPI(
    title="gw-server",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

MAX_TIMEOUT_S = 300.0
ALLOWED_URL_SUFFIX = ".googleapis.com"

DROP_HEADERS = {"authorization", "host", "cookie", "content-length"}


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


class BearerASGIMiddleware:
    """Guard for the mounted MCP app (FastAPI middleware does not apply to mounts)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers") or []}
            auth = headers.get(b"authorization", b"").decode("latin-1")
            ok = auth.startswith("Bearer ") and secrets.compare_digest(
                auth[len("Bearer "):], config.gw_token
            )
            if not ok:
                await JSONResponse(
                    {"ok": False, "error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


# Mount the MCP server LAST — see bottom of file.
def require_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization[len("Bearer "):]
    if not secrets.compare_digest(supplied, config.gw_token):
        raise HTTPException(
            401,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class ExecRequest(BaseModel):
    email: str
    service: str | None = None
    path: str | None = None
    version: str | None = None
    url: str | None = Field(
        default=None,
        description="Full https URL (must end with .googleapis.com). "
        "Mutually exclusive with service/path.",
    )
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT_S)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "gw-server",
        "version": __version__,
        "accounts": len(tokens.list_accounts()),
    }


@app.get("/accounts")
async def accounts(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_bearer(authorization)
    return {"ok": True, "accounts": tokens.list_accounts()}


@app.post("/exec")
async def exec_api(
    req: ExecRequest, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    require_bearer(authorization)

    email = normalize_email(req.email)

    if req.url and (req.service or req.path):
        raise HTTPException(422, detail="pass either 'url' or 'service'+'path', not both")
    if not req.url and not (req.service and req.path):
        raise HTTPException(422, detail="provide 'service'+'path' or a full 'url'")

    if req.url:
        url = req.url
        host = httpx.URL(url).host or ""
        if not url.lower().startswith("https://") or not (
            host == ALLOWED_URL_SUFFIX.lstrip(".") or host.endswith(ALLOWED_URL_SUFFIX)
        ):
            raise HTTPException(422, detail=f"url host must be *{ALLOWED_URL_SUFFIX}")
    else:
        try:
            url = build_url(req.service or "", req.path or "", req.version)
        except TokenError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    try:
        access = await tokens.access_token(email)
    except TokenNotFound as exc:
        return {"ok": False, "email": email, "status": 404, "error": {"message": str(exc)}}
    except TokenError as exc:
        return {"ok": False, "email": email, "status": 502, "error": {"message": str(exc)}}

    extra_headers = {
        k: v
        for k, v in req.headers.items()
        if k.lower() not in DROP_HEADERS
    }
    headers = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        **extra_headers,
    }

    params = {k: v for k, v in req.params.items() if v is not None}

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=req.timeout) as client:
            resp = await client.request(
                req.method, url, params=params, json=req.body, headers=headers
            )
    except httpx.HTTPError as exc:
        log.warning("google call failed email=%s url=%s err=%s", email, url, exc)
        return {
            "ok": False,
            "email": email,
            "status": 502,
            "error": {"message": f"google request failed: {exc}"},
        }
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if "application/json" in resp.headers.get("content-type", ""):
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"raw": resp.text[:10_000]}
    else:
        data = {"raw": resp.text[:10_000]}

    log.info(
        "exec email=%s %s %s -> %s (%dms)",
        email, req.method, url, resp.status_code, elapsed_ms,
    )
    return {
        "ok": resp.is_success,
        "email": email,
        "status": resp.status_code,
        "data": data,
        "elapsedMs": elapsed_ms,
    }


# --------------------------------------------------------------------------
# OAuth login (headless-server friendly)
#
# GET /oauth/login?email=... (Bearer) -> {url}   user opens the URL in ANY browser
# Google -> cloud function (code exchange) -> 302 -> {public_url}/oauth/callback
# GET /oauth/callback -> validate state, verify identity, store token
#
# No local browser or localhost callback required: on a server, set
# GW_PUBLIC_URL=https://gw.trtyr.top and the whole flow works from a link.
# --------------------------------------------------------------------------

_pending_oauth: dict[str, dict[str, Any]] = {}  # csrf -> {email, expires_at, mode, redirect_uri}
OAUTH_PENDING_TTL_S = 600


@app.get("/oauth/login")
async def oauth_login(
    email: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    require_bearer(authorization)
    email = normalize_email(email)

    now = time.monotonic()
    for k in [k for k, v in _pending_oauth.items() if v["expires_at"] < now]:
        _pending_oauth.pop(k, None)

    client = config.oauth_client
    mode = "local" if client else "cloud"
    csrf = secrets.token_hex(32)
    redirect_uri = f"{config.public_url}/oauth/callback"

    if mode == "local":
        # Standard authorization-code flow with OUR client: Google redirects
        # straight back to us with ?code=..., we exchange it for tokens.
        client_id, _ = client  # type: ignore[misc]
        state = csrf
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "access_type": "offline",
                "scope": " ".join(DEFAULT_SCOPES),
                "state": state,
                "prompt": "consent",
            }
        )
    else:
        # Legacy cloud mode via the public geminicli extension client.
        state = base64.b64encode(
            json.dumps({"uri": redirect_uri, "manual": False, "csrf": csrf}).encode()
        ).decode()
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
            {
                "client_id": DEFAULT_CLIENT_ID,
                "redirect_uri": config.cloud_fn_url,
                "response_type": "code",
                "access_type": "offline",
                "scope": " ".join(DEFAULT_SCOPES),
                "state": state,
                "prompt": "consent",
            }
        )

    _pending_oauth[csrf] = {
        "email": email,
        "expires_at": now + OAUTH_PENDING_TTL_S,
        "mode": mode,
        "redirect_uri": redirect_uri,
    }
    log.info("oauth login initiated for %s (mode=%s, callback %s)", email, mode, redirect_uri)
    return {
        "ok": True,
        "email": email,
        "mode": mode,
        "url": auth_url,
        "callbackUrl": redirect_uri,
        "expiresIn": OAUTH_PENDING_TTL_S,
        "hint": "Open this URL in any browser, complete Google consent; "
        "the token lands on this server automatically.",
    }


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> HTMLResponse:
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(request.url.query).items()}
    # The cloud function echoes back the csrf token itself as `state`
    # (not the base64 JSON we originally sent — matches node common.js,
    # which compares returnedState === csrfToken).
    state_raw = qs.get("state", "")
    if state_raw in _pending_oauth:
        csrf = state_raw
    else:
        # fallback: tolerate a full base64-JSON echo
        try:
            csrf = json.loads(base64.b64decode(state_raw)).get("csrf", "")
        except Exception:
            csrf = ""
    pending = _pending_oauth.pop(csrf, None) if csrf else None
    if pending is None or pending["expires_at"] < time.monotonic():
        return HTMLResponse(
            "<h1>❌ Invalid or expired OAuth state</h1><p>Re-initiate login.</p>",
            status_code=400,
        )
    email = pending["email"]
    mode = pending["mode"]

    if qs.get("error"):
        return HTMLResponse(
            f"<h1>❌ Authentication failed: {qs['error']}</h1>", status_code=400
        )

    creds: dict[str, Any]
    if mode == "local":
        # Standard code flow: exchange the authorization code for tokens.
        code = qs.get("code")
        if not code:
            return HTMLResponse("<h1>❌ Callback missing code</h1>", status_code=400)
        client = config.oauth_client
        if not client:
            return HTMLResponse("<h1>❌ Own OAuth client not configured</h1>", status_code=500)
        client_id, client_secret = client
        async with httpx.AsyncClient(timeout=20) as tc:
            tok = await tc.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": pending["redirect_uri"],
                    "grant_type": "authorization_code",
                },
            )
        if tok.status_code >= 400 or not tok.json().get("access_token"):
            return HTMLResponse(
                f"<h1>❌ Token exchange failed</h1><pre>{tok.text[:300]}</pre>",
                status_code=400,
            )
        body = tok.json()
        creds = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "scope": body.get("scope"),
            "token_type": body.get("token_type", "Bearer"),
            "expiry_date": int(time.time() * 1000) + int(body.get("expires_in", 3600)) * 1000,
            "__authMode": "local",
        }
    else:
        access = qs.get("access_token")
        expiry_raw = qs.get("expiry_date")
        if not access or not expiry_raw:
            return HTMLResponse(
                "<h1>❌ Callback did not include tokens</h1>", status_code=400
            )
        creds = {
            "access_token": access,
            "refresh_token": qs.get("refresh_token"),
            "scope": qs.get("scope"),
            "token_type": qs.get("token_type", "Bearer"),
            "expiry_date": int(expiry_raw),
            "__authMode": "cloud",
        }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds['access_token']}"},
        )
    if resp.status_code != 200:
        return HTMLResponse(
            "<h1>❌ Identity check failed</h1>", status_code=400
        )
    identity = normalize_email(str(resp.json().get("email", "")))
    if identity != email:
        return HTMLResponse(
            f"<h1>❌ Authenticated as {identity}, expected {email}.</h1>"
            f"<p>Re-initiate login with --email {identity} or sign in with the right account.</p>",
            status_code=400,
        )

    tokens.save(email, creds)
    log.info("oauth login complete for %s (%s, %d scopes)", email, mode, len((creds.get("scope") or "").split()))
    return HTMLResponse(
        f"<h1>✅ Login successful for {email}</h1>"
        f"<p>Token stored on the server ({mode} mode). You can close this tab.</p>"
    )


# --------------------------------------------------------------------------
# Mount the MCP server (Streamable HTTP) behind bearer auth — LAST, so the
# explicit routes above (/healthz, /accounts, /exec, /oauth/*) win the match;
# everything else falls through to MCP. NOTE: reuse mcp_app — calling
# streamable_http_app() again would create a second, un-started session manager.
app.mount("/", BearerASGIMiddleware(mcp_app))
