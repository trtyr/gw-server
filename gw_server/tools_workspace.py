"""Docs / Sheets / Slides / Chat / Tasks / People tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from .mcp_server import err, gapi_call, mcp, ok, resolve_email

# ---------------------------------------------------------------------------
# Google Docs (scope: documents — full)
# ---------------------------------------------------------------------------


def _docs_extract_text(doc: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in (doc.get("body") or {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        line = "".join(
            (run.get("textRun") or {}).get("content", "")
            for run in para.get("elements", [])
        )
        parts.append(line)
    text = "\n".join(parts).strip()
    return text


@mcp.tool()
async def docs_read(document_id: str, email: str | None = None) -> dict[str, Any]:
    """Read a Google Doc: title + extracted plain text."""
    email = await resolve_email(email)
    result = await gapi_call(email, "docs", f"/documents/{document_id}")
    if not result["ok"]:
        return err(result, email)
    doc = result["data"]
    return ok(email, doc={"id": doc.get("documentId"), "title": doc.get("title"), "text": _docs_extract_text(doc)})


@mcp.tool()
async def docs_create(title: str, text: str | None = None, email: str | None = None) -> dict[str, Any]:
    """Create a Google Doc with a title; optionally insert initial text."""
    email = await resolve_email(email)
    result = await gapi_call(email, "docs", "/documents", method="POST", body={"title": title})
    if not result["ok"]:
        return err(result, email)
    doc_id = result["data"]["documentId"]
    if text:
        ins = await gapi_call(
            email, "docs", f"/documents/{doc_id}:batchUpdate", method="POST",
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]},
        )
        if not ins["ok"]:
            return ok(email, doc={"id": doc_id, "title": title}, warning=f"created but text insert failed: {ins.get('error')}")
    return ok(email, doc={"id": doc_id, "title": title, "text": text})


@mcp.tool()
async def docs_append(document_id: str, text: str, email: str | None = None) -> dict[str, Any]:
    """Append text to the end of a Google Doc body."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "docs", f"/documents/{document_id}:batchUpdate", method="POST",
        body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, appended=len(text))


# ---------------------------------------------------------------------------
# Google Sheets (scope: spreadsheets — read/write)
# ---------------------------------------------------------------------------


@mcp.tool()
async def sheets_read(spreadsheet_id: str, range_: str, email: str | None = None) -> dict[str, Any]:
    """Read values from a Google Sheet. range_ = "SheetName!A1:D20" (or just "A1:D20" for first sheet)."""
    email = await resolve_email(email)
    path = f"/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_, safe='')}"
    result = await gapi_call(email, "sheets", path, params={"majorDimension": "ROWS"})
    if not result["ok"]:
        return err(result, email)
    return ok(email, values=result["data"].get("values", []), range_=result["data"].get("range"))


@mcp.tool()
async def sheets_list(spreadsheet_id: str, email: str | None = None) -> dict[str, Any]:
    """List sheet tabs and grid info of a spreadsheet (sheet titles for ranges)."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "sheets", f"/spreadsheets/{spreadsheet_id}",
        params={"fields": "properties.title,sheets.properties"},
    )
    if not result["ok"]:
        return err(result, email)
    props = (result["data"].get("properties") or {})
    return ok(email, spreadsheet={
        "title": props.get("title"),
        "sheets": [
            {
                "title": (s.get("properties") or {}).get("title"),
                "rows": (s.get("properties") or {}).get("gridProperties", {}).get("rowCount"),
                "cols": (s.get("properties") or {}).get("gridProperties", {}).get("columnCount"),
            }
            for s in result["data"].get("sheets", [])
        ],
    })


@mcp.tool()
async def sheets_write(spreadsheet_id: str, range_: str, values: list[list[Any]], email: str | None = None) -> dict[str, Any]:
    """Write values (2D array) into a Google Sheet range, overwriting. Use USER_ENTERED so formulas/numbers are interpreted."""
    email = await resolve_email(email)
    path = f"/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_, safe='')}"
    result = await gapi_call(
        email, "sheets", path, method="PUT",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": values},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, updatedCells=result["data"].get("updatedCells"), range_=result["data"].get("updatedRange"))


@mcp.tool()
async def sheets_append(spreadsheet_id: str, range_: str, values: list[list[Any]], email: str | None = None) -> dict[str, Any]:
    """Append rows to a Google Sheet table (range_ identifies the table, e.g. "Sheet1!A:D")."""
    email = await resolve_email(email)
    path = f"/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range_, safe='')}:append"
    result = await gapi_call(
        email, "sheets", path, method="POST",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        body={"values": values},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, updatedCells=result["data"].get("updates", {}).get("updatedCells"), range_=result["data"].get("tableRange"))


# ---------------------------------------------------------------------------
# Google Slides (scope: presentations.readonly — read only)
# ---------------------------------------------------------------------------


@mcp.tool()
async def slides_read(presentation_id: str, email: str | None = None) -> dict[str, Any]:
    """Read a Google Slides presentation: title + all text content per slide."""
    email = await resolve_email(email)
    result = await gapi_call(email, "slides", f"/presentations/{presentation_id}")
    if not result["ok"]:
        return err(result, email)
    pres = result["data"]
    slides = []
    for slide in pres.get("slides", []):
        texts: list[str] = []
        for element in slide.get("pageElements", []):
            shape_text = (
                ((element.get("shape") or {}).get("text") or {}).get("textElements") or []
            )
            for te in shape_text:
                if "textRun" in te:
                    texts.append(te["textRun"].get("content", ""))
        slides.append({"slide": slide.get("objectId"), "text": "".join(texts).strip()})
    return ok(email, presentation={"id": pres.get("presentationId"), "title": pres.get("title"), "slides": slides})


# ---------------------------------------------------------------------------
# Google Chat (scopes: chat.spaces / chat.messages / chat.memberships)
# ---------------------------------------------------------------------------


@mcp.tool()
async def chat_spaces_list(email: str | None = None, max_results: int = 25) -> dict[str, Any]:
    """List Google Chat spaces the account is in. Returns space names ("spaces/XXX") — needed for chat_messages_list / chat_send."""
    email = await resolve_email(email)
    result = await gapi_call(email, "chat", "/spaces", params={"pageSize": max(1, min(max_results, 100))})
    if not result["ok"]:
        return err(result, email)
    return ok(email, spaces=[{"name": s.get("name"), "displayName": s.get("displayName"), "type": s.get("spaceType")} for s in result["data"].get("spaces", [])])


@mcp.tool()
async def chat_messages_list(space_name: str, email: str | None = None, max_results: int = 20) -> dict[str, Any]:
    """List recent messages in a Chat space. space_name = "spaces/XXX" from chat_spaces_list."""
    email = await resolve_email(email)
    path = f"/{space_name}/messages"
    result = await gapi_call(email, "chat", path, params={"pageSize": max(1, min(max_results, 100))})
    if not result["ok"]:
        return err(result, email)
    return ok(email, messages=[
        {
            "name": msg.get("name"),
            "sender": ((msg.get("sender") or {}).get("user") or {}).get("displayName"),
            "createTime": msg.get("createTime"),
            "text": msg.get("text"),
        }
        for msg in result["data"].get("messages", [])
    ])


@mcp.tool()
async def chat_send(space_name: str, text: str, email: str | None = None) -> dict[str, Any]:
    """Send a text message to a Google Chat space. space_name = "spaces/XXX" from chat_spaces_list."""
    email = await resolve_email(email)
    result = await gapi_call(email, "chat", f"/{space_name}/messages", method="POST", body={"text": text})
    if not result["ok"]:
        return err(result, email)
    return ok(email, sent={"name": result["data"].get("name"), "text": result["data"].get("text")})


# ---------------------------------------------------------------------------
# Google Tasks (scope: tasks)
# ---------------------------------------------------------------------------


@mcp.tool()
async def tasks_lists(email: str | None = None) -> dict[str, Any]:
    """List Google Tasks task lists (ids for tasks_list / tasks_add)."""
    email = await resolve_email(email)
    result = await gapi_call(email, "tasks", "/users/@me/lists")
    if not result["ok"]:
        return err(result, email)
    return ok(email, lists=[{"id": l.get("id"), "title": l.get("title")} for l in result["data"].get("items", [])])


@mcp.tool()
async def tasks_list(email: str | None = None, list_id: str = "@default", show_completed: bool = False, max_results: int = 50) -> dict[str, Any]:
    """List tasks in a task list (@default = default list). Shows pending tasks by default."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "tasks", f"/lists/{list_id}/tasks",
        params={"showCompleted": show_completed, "showHidden": False, "maxResults": max(1, min(max_results, 100))},
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, tasks=[
        {
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "due": t.get("due"),
            "notes": t.get("notes"),
        }
        for t in result["data"].get("items", [])
    ])


@mcp.tool()
async def tasks_add(
    title: str, email: str | None = None, due: str | None = None,
    notes: str | None = None, list_id: str = "@default",
) -> dict[str, Any]:
    """Add a task. due is RFC3339 e.g. "2026-09-30T18:00:00+08:00" or "2026-09-30T00:00:00Z"."""
    email = await resolve_email(email)
    body: dict[str, Any] = {"title": title}
    if due:
        body["due"] = due
    if notes:
        body["notes"] = notes
    result = await gapi_call(email, "tasks", f"/lists/{list_id}/tasks", method="POST", body=body)
    if not result["ok"]:
        return err(result, email)
    return ok(email, task={"id": result["data"].get("id"), "title": result["data"].get("title"), "due": result["data"].get("due")})


@mcp.tool()
async def tasks_complete(task_id: str, email: str | None = None, list_id: str = "@default") -> dict[str, Any]:
    """Mark a task completed (or un-complete with completed=false via tasks_update-style patch here: pass completed=true)."""
    email = await resolve_email(email)
    result = await gapi_call(email, "tasks", f"/lists/{list_id}/tasks/{task_id}", method="PATCH", body={"status": "completed"})
    if not result["ok"]:
        return err(result, email)
    return ok(email, task={"id": result["data"].get("id"), "status": result["data"].get("status")})


# ---------------------------------------------------------------------------
# Contacts / directory (scope: contacts.readonly)
# ---------------------------------------------------------------------------


@mcp.tool()
async def people_search(query: str, email: str | None = None, max_results: int = 10) -> dict[str, Any]:
    """Search the account's contacts by name/email/phone. Returns names + email addresses — handy before gmail_send."""
    email = await resolve_email(email)
    contacts = await gapi_call(
        email, "people", "/people:searchContacts",
        params={"query": query, "readMask": "names,emailAddresses", "pageSize": max(1, min(max_results, 30))},
    )
    out: list[dict[str, Any]] = []
    if contacts["ok"]:
        for p in contacts["data"].get("results", []):
            person = p.get("person") or {}
            out.append({
                "name": ((person.get("names") or [{}])[0]).get("displayName"),
                "emails": [e.get("value") for e in person.get("emailAddresses", [])],
                "source": "contacts",
            })
    return ok(email, results=out[:max_results])
