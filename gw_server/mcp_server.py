"""MCP tool surface core: server instance + shared helpers.

Tool modules (tools_gmail / tools_drive / tools_calendar / tools_workspace)
register their tools onto the same `mcp` instance and are imported at the
bottom of this file.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from .config import config
from .tokens import DEFAULT_VERSIONS, TokenError, TokenManager, TokenNotFound, build_url, normalize_email

mcp = MCPServer(
    "gw",
    instructions=(
        "Google Workspace toolset: Gmail / Drive / Calendar / Docs / Sheets / Slides / "
        "Chat / Tasks / Contacts. Multi-account: pass `email` to pick an account; omit it "
        "when only one account is signed in. `gapi` is the generic escape hatch for any "
        "Google REST endpoint; the named tools cover common operations."
    ),
)

tokens = TokenManager(config.tokens_dir, forced_mode=config.auth_mode)

MAX_TEXT = 100_000  # max chars of file/mail body returned to the client


# ---------------------------------------------------------------------------
# helpers (shared by tool modules)
# ---------------------------------------------------------------------------


async def resolve_email(email: str | None) -> str:
    if email:
        return normalize_email(email)
    accounts = tokens.list_accounts()
    if len(accounts) == 1:
        return accounts[0]["email"]
    if not accounts:
        raise ValueError(
            "no accounts signed in on the server; initiate login via /oauth/login"
        )
    raise ValueError(f"multiple accounts signed in, pass email: {[a['email'] for a in accounts]}")


async def gapi_call(
    email: str,
    service: str,
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: Any = None,
    version: str | None = None,
    raw_body: bytes | None = None,
    raw_content_type: str | None = None,
) -> dict[str, Any]:
    """In-process Google REST call with auto-refreshed OAuth token."""
    access = await tokens.access_token(email)
    url = build_url(service, path, version)
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
    kwargs: dict[str, Any] = {"params": clean_params, "headers": headers}
    if raw_body is not None:
        kwargs["content"] = raw_body
        if raw_content_type:
            headers["Content-Type"] = raw_content_type
    elif body is not None:
        kwargs["json"] = body
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.request(method, url, **kwargs)
    if "application/json" in resp.headers.get("content-type", ""):
        try:
            data: Any = resp.json()
        except ValueError:
            data = {"raw": resp.text[:10_000]}
    else:
        data = {"raw": resp.text[:10_000]}

    result: dict[str, Any] = {"ok": resp.is_success, "status": resp.status_code}
    if resp.is_success:
        result["data"] = data
    else:
        result["error"] = data
    return result


def ok(email: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "email": email, **fields}


def err(result: dict[str, Any], email: str) -> dict[str, Any]:
    return {
        "ok": False,
        "email": email,
        "status": result.get("status"),
        "error": result.get("error") or result.get("data"),
    }


def decode_b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    except Exception:
        return "(undecodable)"


def extract_text_body(payload: dict[str, Any]) -> str:
    """Best-effort plain-text extraction from a Gmail message payload."""

    def walk(node: dict[str, Any]) -> str | None:
        mime = node.get("mimeType", "")
        if mime == "text/plain" and (node.get("body") or {}).get("data"):
            return decode_b64url(node["body"]["data"])
        for child in node.get("parts", []) or []:
            text = walk(child)
            if text:
                return text
        if mime == "text/html" and (node.get("body") or {}).get("data"):
            return decode_b64url(node["body"]["data"])
        return None

    return (walk(payload or {}) or "(no text body)")[:MAX_TEXT]


def _iter_parts(node: dict[str, Any]):
    yield node
    for child in node.get("parts", []) or []:
        yield from _iter_parts(child)


def message_summary(m: dict[str, Any]) -> dict[str, Any]:
    headers = {
        h["name"].lower(): h["value"]
        for h in (m.get("payload") or {}).get("headers", [])
    }
    attachments = []
    for part in _iter_parts(m.get("payload") or {}):
        if part.get("filename") and (part.get("body") or {}).get("attachmentId"):
            attachments.append(
                {
                    "filename": part["filename"],
                    "attachmentId": part["body"]["attachmentId"],
                    "mimeType": part.get("mimeType"),
                    "size": (part.get("body") or {}).get("size"),
                }
            )
    return {
        "id": m.get("id"),
        "threadId": m.get("threadId"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "snippet": m.get("snippet"),
        "labels": m.get("labelIds"),
        "attachments": attachments,
    }


# ---------------------------------------------------------------------------
# core tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def accounts_list() -> dict[str, Any]:
    """List Google accounts signed in on this server (no secrets)."""
    return {"ok": True, "accounts": tokens.list_accounts()}


@mcp.tool()
async def whoami(email: str | None = None) -> dict[str, Any]:
    """Return the Google identity (email, name, picture) for an account."""
    email = await resolve_email(email)
    result = await gapi_call(email, "oauth2", "/userinfo")
    if not result["ok"]:
        return err(result, email)
    return ok(email, identity=result["data"])


@mcp.tool()
async def gapi(
    service: str,
    path: str,
    email: str | None = None,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: Any = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Generic escape hatch: call ANY Google Workspace REST endpoint.

    Args:
        service: gmail/drive/calendar/docs/sheets/slides/people/chat/tasks/oauth2/admin
        path: REST path, e.g. "/users/me/labels", "/documents/<docId>". Quote special chars.
        method: GET/POST/PUT/PATCH/DELETE
        params: query parameters
        body: JSON request body
        version: API version override (defaults: gmail v1, drive v3, calendar v3, docs v1, sheets v4, slides v1, chat v1, tasks v1, people v1)
    """
    email = await resolve_email(email)
    result = await gapi_call(email, service, path, method, params, body, version)
    if result["ok"]:
        return ok(email, data=result["data"])
    return err(result, email)


# import tool modules — each registers tools on the shared mcp instance
from . import tools_gmail, tools_drive, tools_calendar, tools_workspace  # noqa: E402,F401

__all__ = [
    "mcp", "tokens", "gapi_call", "resolve_email", "ok", "err",
    "decode_b64url", "extract_text_body", "message_summary", "MAX_TEXT",
]
