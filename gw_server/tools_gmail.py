"""Gmail tools (scope: gmail.modify — read/search/send/drafts/labels/attachments)."""

from __future__ import annotations

import base64
from typing import Any

from .mcp_server import (
    err,
    extract_text_body,
    gapi_call,
    mcp,
    message_summary,
    ok,
    resolve_email,
)


@mcp.tool()
async def gmail_search(
    query: str = "in:inbox",
    email: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search Gmail messages. `query` is Gmail search syntax, e.g. "from:alice", "has:attachment newer_than:7d", "is:unread", "subject:report". Returns message summaries; use gmail_read for full content."""
    email = await resolve_email(email)
    result = await gapi_call(
        email,
        "gmail",
        "/users/me/messages",
        params={"q": query, "maxResults": max(1, min(max_results, 50))},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, messages=result["data"].get("messages", []))


@mcp.tool()
async def gmail_read(message_id: str, email: str | None = None) -> dict[str, Any]:
    """Read one Gmail message in full: headers, snippet, extracted text body, and attachment list (use gmail_attachment_get for content)."""
    email = await resolve_email(email)
    result = await gapi_call(email, "gmail", f"/users/me/messages/{message_id}")
    if not result["ok"]:
        return err(result, email)
    full = result["data"]
    return ok(email, message={**message_summary(full), "body": extract_text_body(full.get("payload"))})


@mcp.tool()
async def gmail_send(to: str, subject: str, body: str, email: str | None = None) -> dict[str, Any]:
    """Send an email (plain text) from the signed-in account. `to` accepts one address."""
    email = await resolve_email(email)
    raw = _mime(to, subject, body)
    result = await gapi_call(
        email, "gmail", "/users/me/messages/send", method="POST",
        body={"raw": raw},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, sent={"id": result["data"].get("id"), "to": to, "subject": subject})


@mcp.tool()
async def gmail_reply(
    message_id: str, body: str, email: str | None = None, send: bool = True,
) -> dict[str, Any]:
    """Reply to a message within its thread. Inherits recipient/subject; set send=false to save as draft instead."""
    email = await resolve_email(email)
    orig = await gapi_call(email, "gmail", f"/users/me/messages/{message_id}")
    if not orig["ok"]:
        return err(orig, email)
    headers = {h["name"].lower(): h["value"] for h in (orig["data"].get("payload") or {}).get("headers", [])}
    to = headers.get("reply-to") or headers.get("from", "")
    subject = headers.get("subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg_id = headers.get("message-id", "")
    refs = " ".join(x for x in [headers.get("references", ""), msg_id] if x)
    raw = _mime(to, subject, body, in_reply_to=msg_id, references=refs)
    payload: dict[str, Any] = {"raw": raw, "threadId": orig["data"].get("threadId")}
    path = "/users/me/messages/send" if send else "/users/me/drafts"
    result = await gapi_call(email, "gmail", path, method="POST", body=payload)
    if not result["ok"]:
        return err(result, email)
    return ok(email, result=result["data"], to=to, subject=subject)


@mcp.tool()
async def gmail_draft_create(to: str, subject: str, body: str, email: str | None = None) -> dict[str, Any]:
    """Create a Gmail draft (plain text)."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "gmail", "/users/me/drafts", method="POST",
        body={"message": {"raw": _mime(to, subject, body)}},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, draft={"id": result["data"].get("id"), "to": to, "subject": subject})


@mcp.tool()
async def gmail_draft_list(email: str | None = None, max_results: int = 10) -> dict[str, Any]:
    """List Gmail drafts (ids + snippets)."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "gmail", "/users/me/drafts", params={"maxResults": max(1, min(max_results, 50))}
    )
    if not result["ok"]:
        return err(result, email)
    drafts = result["data"].get("drafts", [])
    return ok(email, drafts=[{"id": d.get("id"), "message": message_summary(d.get("message") or {})} for d in drafts])


@mcp.tool()
async def gmail_draft_send(draft_id: str, email: str | None = None) -> dict[str, Any]:
    """Send an existing Gmail draft by id."""
    email = await resolve_email(email)
    result = await gapi_call(email, "gmail", "/users/me/drafts/send", method="POST", body={"id": draft_id})
    if not result["ok"]:
        return err(result, email)
    return ok(email, sent={"id": result["data"].get("id")})


@mcp.tool()
async def gmail_modify_labels(
    message_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Add/remove labels on a message. Common labels: INBOX, TRASH, STARRED, UNREAD, SPAM. E.g. archive = remove INBOX; mark unread = add UNREAD; delete-ish = add TRASH."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "gmail", f"/users/me/messages/{message_id}/modify", method="POST",
        body={"addLabelIds": add_labels or [], "removeLabelIds": remove_labels or []},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, message={"id": result["data"].get("id"), "labels": result["data"].get("labelIds")})


@mcp.tool()
async def gmail_labels_list(email: str | None = None) -> dict[str, Any]:
    """List all Gmail labels (system + user) with ids."""
    email = await resolve_email(email)
    result = await gapi_call(email, "gmail", "/users/me/labels")
    if not result["ok"]:
        return err(result, email)
    return ok(email, labels=[{"id": l["id"], "name": l["name"], "type": l.get("type")} for l in result["data"].get("labels", [])])


@mcp.tool()
async def gmail_attachment_get(
    message_id: str, attachment_id: str, email: str | None = None,
) -> dict[str, Any]:
    """Fetch an attachment's content (base64url-decoded text; binary files are returned as base64). Use ids from gmail_read's attachment list."""
    email = await resolve_email(email)
    result = await gapi_call(email, "gmail", f"/users/me/messages/{message_id}/attachments/{attachment_id}")
    if not result["ok"]:
        return err(result, email)
    data = result["data"].get("data", "")
    decoded = base64.urlsafe_b64decode(data + "===")
    try:
        content: Any = decoded.decode("utf-8")[:200_000]
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(decoded).decode("ascii")[:200_000]
        encoding = "base64"
    return ok(email, attachment={"size": result["data"].get("size"), "encoding": encoding, "content": content})


def _mime(
    to: str, subject: str, body: str,
    in_reply_to: str | None = None, references: str | None = None,
) -> str:
    lines = [f"To: {to}", f"Subject: {subject}"]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        lines.append(f"References: {references}")
    lines.append('Content-Type: text/plain; charset="UTF-8"')
    lines.append("")
    lines.append(body)
    return base64.urlsafe_b64encode("\r\n".join(lines).encode("utf-8")).decode("ascii")
