"""Google Drive tools (scope: drive — full read/write/share)."""

from __future__ import annotations

import urllib.parse
from typing import Any

from .mcp_server import MAX_TEXT, err, gapi_call, mcp, ok, resolve_email

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"
FOLDER_MIME = "application/vnd.google-apps.folder"

EXPORT_MIME = {
    GOOGLE_DOC_MIME: "text/plain",
    GOOGLE_SHEET_MIME: "text/csv",
    GOOGLE_SLIDE_MIME: "text/plain",
}


@mcp.tool()
async def drive_search(
    query: str = "",
    email: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search Google Drive files. `query` is Drive search syntax, e.g. "name contains 'report'", "mimeType='application/vnd.google-apps.spreadsheet'", "fullText contains 'q3'", "'<folderId>' in parents". Empty lists recently modified."""
    email = await resolve_email(email)
    params: dict[str, Any] = {
        "pageSize": max(1, min(max_results, 50)),
        "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink,owners(displayName))",
        "orderBy": "modifiedTime desc",
    }
    if query:
        params["q"] = query
    result = await gapi_call(email, "drive", "/files", params=params)
    if not result["ok"]:
        return err(result, email)
    return ok(email, files=result["data"].get("files", []))


@mcp.tool()
async def drive_list(folder_id: str | None = None, email: str | None = None, max_results: int = 20) -> dict[str, Any]:
    """List files inside a Drive folder (omit folder_id to list My Drive root)."""
    email = await resolve_email(email)
    q = f"'{folder_id}' in parents and trashed=false" if folder_id else "trashed=false"
    result = await gapi_call(
        email, "drive", "/files",
        params={
            "q": q,
            "pageSize": max(1, min(max_results, 100)),
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
        },
    )
    if not result["ok"]:
        return err(result, email)
    return ok(email, files=result["data"].get("files", []))


@mcp.tool()
async def drive_read(file_id: str, email: str | None = None, as_markdown: bool = False) -> dict[str, Any]:
    """Read a Drive file's content. Google Docs/Sheets/Slides are exported to text/markdown/docs, csv/sheets, text/slides automatically; regular files are returned as text (binary as base64), truncated at ~100KB."""
    email = await resolve_email(email)
    meta = await gapi_call(email, "drive", f"/files/{file_id}", params={"fields": "id,name,mimeType,size"})
    if not meta["ok"]:
        return err(meta, email)
    name = meta["data"].get("name")
    mime = meta["data"].get("mimeType", "")

    if mime in EXPORT_MIME:
        export_mime = EXPORT_MIME[mime]
        if as_markdown and mime == GOOGLE_DOC_MIME:
            export_mime = "text/markdown"
        body = await _download(email, f"/files/{file_id}/export", params={"mimeType": export_mime})
        kind = f"exported({export_mime})"
    else:
        body = await _download(email, f"/files/{file_id}", params={"alt": "media"})
        kind = "raw"

    if isinstance(body, dict):  # error passthrough
        return err({"status": body.get("status"), "error": body.get("error")}, email)
    try:
        content: Any = body.decode("utf-8")[:MAX_TEXT]
        encoding = "utf-8"
    except UnicodeDecodeError:
        import base64 as b64
        content = b64.b64encode(body).decode("ascii")[:MAX_TEXT]
        encoding = "base64"
    return ok(email, file={"id": file_id, "name": name, "mimeType": mime, "kind": kind, "encoding": encoding, "content": content})


async def _download(email: str, path: str, params: dict[str, Any]) -> bytes | dict[str, Any]:
    """Raw download via drive API (export or alt=media). Returns bytes or error dict."""
    import httpx
    from .tokens import build_url
    url = build_url("drive", path, "v3")
    token = await _token(email)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code >= 400:
        return {"status": resp.status_code, "error": {"message": resp.text[:500]}}
    return resp.content


@mcp.tool()
async def drive_download(file_id: str, email: str | None = None) -> dict[str, Any]:
    """Download a regular (non-Google-Docs) Drive file's raw content as base64 (for binary) or utf-8 text."""
    email = await resolve_email(email)
    result = await gapi_call(email, "drive", f"/files/{file_id}", params={"alt": "media"})
    if not result["ok"]:
        return err(result, email)
    return ok(email, data=result["data"])


@mcp.tool()
async def drive_upload(
    name: str, content: str, email: str | None = None,
    mime_type: str = "text/plain", folder_id: str | None = None,
) -> dict[str, Any]:
    """Create a new Drive file with text content (plain text, csv, json, markdown, html...). Google-Docs-native types cannot be created this way."""
    email = await resolve_email(email)
    meta: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if folder_id:
        meta["parents"] = [folder_id]
    created = await gapi_call(email, "drive", "/files", method="POST", body=meta)
    if not created["ok"]:
        return err(created, email)
    file_id = created["data"]["id"]
    import httpx
    from .tokens import build_url
    url = build_url("drive", f"/files/{file_id}", "v3").replace(
        "https://www.googleapis.com", "https://www.googleapis.com/upload", 1
    )
    token = await _token(email)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.patch(
            url, params={"uploadType": "media"}, content=content.encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime_type},
        )
    if resp.status_code >= 400:
        return {"ok": False, "email": email, "status": resp.status_code, "error": resp.text[:500]}
    return ok(email, file={"id": file_id, "name": name, "webViewLink": created["data"].get("webViewLink")})


@mcp.tool()
async def drive_update(file_id: str, content: str, email: str | None = None, mime_type: str = "text/plain") -> dict[str, Any]:
    """Overwrite a regular Drive file's content (not for Google-Docs-native types)."""
    email = await resolve_email(email)
    import httpx
    from .tokens import build_url
    url = build_url("drive", f"/files/{file_id}", "v3").replace(
        "https://www.googleapis.com", "https://www.googleapis.com/upload", 1
    )
    token = await _token(email)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.patch(
            url, params={"uploadType": "media"}, content=content.encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime_type},
        )
    if resp.status_code >= 400:
        return {"ok": False, "email": email, "status": resp.status_code, "error": resp.text[:500]}
    return ok(email, updated={"id": file_id})


@mcp.tool()
async def drive_create_folder(name: str, email: str | None = None, parent_id: str | None = None) -> dict[str, Any]:
    """Create a Drive folder (optionally inside parent_id)."""
    email = await resolve_email(email)
    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    result = await gapi_call(email, "drive", "/files", method="POST", body=body)
    if not result["ok"]:
        return err(result, email)
    return ok(email, folder={"id": result["data"].get("id"), "name": name})


@mcp.tool()
async def drive_move(
    file_id: str, email: str | None = None,
    add_parent: str | None = None, remove_parent: str | None = None, new_name: str | None = None,
) -> dict[str, Any]:
    """Move and/or rename a Drive file. Provide add_parent/remove_parent folder ids; when moving, pass the current parent as remove_parent."""
    email = await resolve_email(email)
    params: dict[str, Any] = {}
    if add_parent:
        params["addParents"] = add_parent
    if remove_parent:
        params["removeParents"] = remove_parent
    body: dict[str, Any] = {}
    if new_name:
        body["name"] = new_name
    result = await gapi_call(email, "drive", f"/files/{file_id}", method="PATCH", params=params, body=body)
    if not result["ok"]:
        return err(result, email)
    return ok(email, file={"id": file_id, "name": result["data"].get("name"), "parents": result["data"].get("parents")})


@mcp.tool()
async def drive_trash(file_id: str, email: str | None = None, permanently: bool = False) -> dict[str, Any]:
    """Trash a Drive file (permanently=true deletes forever — most files cannot be permanently deleted by this API)."""
    email = await resolve_email(email)
    if permanently:
        result = await gapi_call(email, "drive", f"/files/{file_id}", method="DELETE")
    else:
        result = await gapi_call(email, "drive", f"/files/{file_id}", method="PATCH", body={"trashed": True})
    if not result["ok"]:
        return err(result, email)
    return ok(email, trashed=file_id)


@mcp.tool()
async def drive_share(
    file_id: str, email_address: str, email: str | None = None,
    role: str = "reader",
) -> dict[str, Any]:
    """Share a Drive file with someone. role: reader/writer/organizer. Notification email is not sent."""
    email_acc = await resolve_email(email)
    result = await gapi_call(
        email_acc, "drive", f"/files/{file_id}/permissions", method="POST",
        params={"sendNotificationEmail": False},
        body={"type": "user", "role": role, "emailAddress": email_address},
    )
    if not result["ok"]:
        return err(result, email_acc)
    return ok(email_acc, shared={"file_id": file_id, "with": email_address, "role": role})


async def _token(email: str) -> str:
    from .mcp_server import tokens
    return await tokens.access_token(email)
