"""MCP tool surface — what this service actually DOES for AI clients.

pi (or any MCP client) connects via Streamable HTTP + Bearer and discovers
these tools directly: no REST knowledge, no googleapis, no node CLI.
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
        "Google Workspace toolset (Gmail / Drive / Calendar / Docs / Chat / Sheets / Slides). "
        "Multi-account: pass `email` to pick an account; omit it when only one account is signed in. "
        "`gapi` is the generic escape hatch for any Google REST endpoint; the named tools cover "
        "the common operations."
    ),
)

tokens = TokenManager(config.tokens_dir, forced_mode=config.auth_mode)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _resolve_email(email: str | None) -> str:
    if email:
        return normalize_email(email)
    accounts = tokens.list_accounts()
    if len(accounts) == 1:
        return accounts[0]["email"]
    if not accounts:
        raise ValueError(
            "no accounts signed in on the server; run `uv run python -m gw_server.login --email <addr>` on the host"
        )
    raise ValueError(f"multiple accounts signed in, pass email: {[a['email'] for a in accounts]}")


async def _gapi(
    email: str,
    service: str,
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: Any = None,
    version: str | None = None,
) -> dict[str, Any]:
    """In-process Google REST call with auto-refreshed OAuth token."""
    access = await tokens.access_token(email)
    url = build_url(service, path, version)
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            url,
            params=clean_params,
            json=body,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        )
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


def _extract_text_body(payload: dict[str, Any]) -> str:
    """Best-effort plain-text extraction from a Gmail message payload."""

    def decode(part: dict[str, Any]) -> str | None:
        data = (part.get("body") or {}).get("data")
        if not data:
            return None
        try:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
        except Exception:
            return None

    def walk(node: dict[str, Any]) -> str | None:
        mime = node.get("mimeType", "")
        if mime == "text/plain":
            text = decode(node)
            if text:
                return text
        for child in node.get("parts", []) or []:
            text = walk(child)
            if text:
                return text
        if mime == "text/html":
            return decode(node)
        return None

    return walk(payload or {}) or "(no text body)"


def _message_summary(m: dict[str, Any]) -> dict[str, Any]:
    headers = {
        h["name"].lower(): h["value"]
        for h in (m.get("payload") or {}).get("headers", [])
    }
    return {
        "id": m.get("id"),
        "threadId": m.get("threadId"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "snippet": m.get("snippet"),
        "labels": m.get("labelIds"),
    }


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def accounts_list() -> dict[str, Any]:
    """List Google accounts signed in on this server (no secrets)."""
    accounts = tokens.list_accounts()
    return {"ok": True, "accounts": accounts}


@mcp.tool()
async def whoami(email: str | None = None) -> dict[str, Any]:
    """Return the Google identity (email, name, picture) for an account."""
    email = await _resolve_email(email)
    result = await _gapi(email, "oauth2", "/userinfo")
    if not result["ok"]:
        return result
    return {"ok": True, "email": email, "identity": result["data"]}


@mcp.tool()
async def gmail_search(
    query: str = "in:inbox",
    email: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search Gmail messages. `query` is Gmail search syntax (e.g. "from:alice", "has:attachment newer_than:7d", "in:inbox"). Returns summaries; follow up with gmail_read for full content."""
    email = await _resolve_email(email)
    result = await _gapi(
        email,
        "gmail",
        "/users/me/messages",
        params={"q": query, "maxResults": max(1, min(max_results, 50))},
    )
    if not result["ok"]:
        return result
    return {"ok": True, "email": email, "messages": result["data"].get("messages", [])}


@mcp.tool()
async def gmail_read(message_id: str, email: str | None = None) -> dict[str, Any]:
    """Read one Gmail message in full: headers, snippet and extracted text body."""
    email = await _resolve_email(email)
    result = await _gapi(email, "gmail", f"/users/me/messages/{message_id}")
    if not result["ok"]:
        return result
    full = result["data"]
    return {
        "ok": True,
        "email": email,
        "message": {**_message_summary(full), "body": _extract_text_body(full.get("payload"))},
    }


@mcp.tool()
async def gmail_send(to: str, subject: str, body: str, email: str | None = None) -> dict[str, Any]:
    """Send an email (plain text) from the signed-in account."""
    email = await _resolve_email(email)
    mime = (
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        'Content-Type: text/plain; charset="UTF-8"\r\n\r\n'
        f"{body}"
    )
    raw = base64.urlsafe_b64encode(mime.encode("utf-8")).decode("ascii")
    result = await _gapi(email, "gmail", "/users/me/messages/send", method="POST", body={"raw": raw})
    if not result["ok"]:
        return result
    return {"ok": True, "email": email, "sent": {"id": result["data"].get("id"), "to": to, "subject": subject}}


@mcp.tool()
async def drive_search(
    query: str = "",
    email: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search Google Drive files. `query` is Drive search syntax (e.g. "name contains 'report'", "mimeType='application/vnd.google-apps.document'"); empty lists recent files."""
    email = await _resolve_email(email)
    params: dict[str, Any] = {
        "pageSize": max(1, min(max_results, 50)),
        "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
        "orderBy": "modifiedTime desc",
    }
    if query:
        params["q"] = query
    result = await _gapi(email, "drive", "/files", params=params)
    if not result["ok"]:
        return result
    return {"ok": True, "email": email, "files": result["data"].get("files", [])}


@mcp.tool()
async def calendar_events(
    email: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
    max_results: int = 20,
) -> dict[str, Any]:
    """List primary-calendar events between ISO datetimes `time_min`/`time_max` (e.g. "2026-09-28T00:00:00+08:00"). Both optional — defaults cover now to +7 days."""
    from datetime import datetime, timedelta, timezone

    email = await _resolve_email(email)
    now = datetime.now(timezone.utc)
    tmin = time_min or (now - timedelta(hours=1)).isoformat()
    tmax = time_max or (now + timedelta(days=7)).isoformat()
    result = await _gapi(
        email,
        "calendar",
        "/calendars/primary/events",
        params={
            "timeMin": tmin,
            "timeMax": tmax,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 50)),
        },
    )
    if not result["ok"]:
        return result
    events = [
        {
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
            "location": e.get("location"),
            "hangoutLink": e.get("hangoutLink"),
        }
        for e in result["data"].get("items", [])
    ]
    return {"ok": True, "email": email, "events": events}


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
        path: REST path, e.g. "/users/me/labels", "/documents/<docId>"
        method: GET/POST/PUT/PATCH/DELETE
        params: query parameters
        body: JSON request body
        version: API version override (defaults: gmail v1, drive v3, calendar v3, docs v1, sheets v4, ...)
    """
    email = await _resolve_email(email)
    result = await _gapi(email, service, path, method, params, body, version)
    if result["ok"]:
        return {"ok": True, "email": email, "data": result["data"]}
    return {"ok": False, "email": email, "status": result["status"], "error": result.get("error")}


__all__ = ["mcp", "tokens"]
