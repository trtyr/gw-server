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

import logging
import secrets
import time
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .config import config
from .tokens import (
    SERVICE_ROUTES,
    TokenError,
    TokenManager,
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

tokens = TokenManager(config.tokens_dir, forced_mode=config.auth_mode)

app = FastAPI(
    title="gw-server",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

MAX_TIMEOUT_S = 300.0
ALLOWED_URL_SUFFIX = ".googleapis.com"

DROP_HEADERS = {"authorization", "host", "cookie", "content-length"}


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

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
