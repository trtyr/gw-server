"""Google Calendar tools (scope: calendar — full CRUD + freebusy)."""

from __future__ import annotations

from typing import Any

from .mcp_server import err, gapi_call, mcp, ok, resolve_email


@mcp.tool()
async def calendar_events(
    email: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
    calendar_id: str = "primary",
    max_results: int = 20,
) -> dict[str, Any]:
    """List calendar events between ISO datetimes `time_min`/`time_max` (e.g. "2026-09-28T00:00:00+08:00"). Both optional — defaults cover now-1h to +7d. Use calendar_id for shared calendars (see calendar_calendars_list)."""
    email = await resolve_email(email)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    result = await gapi_call(
        email, "calendar", f"/calendars/{calendar_id}/events",
        params={
            "timeMin": time_min or (now - timedelta(hours=1)).isoformat(),
            "timeMax": time_max or (now + timedelta(days=7)).isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 50)),
        },
    )
    if not result["ok"]:
        return err(result, email)
    events = [
        {
            "id": e.get("id"),
            "summary": e.get("summary"),
            "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
            "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
            "location": e.get("location"),
            "hangoutLink": e.get("hangoutLink"),
            "organizer": (e.get("organizer") or {}).get("email"),
        }
        for e in result["data"].get("items", [])
    ]
    return ok(email, events=events)


@mcp.tool()
async def calendar_create(
    summary: str, start: str, end: str, email: str | None = None,
    description: str | None = None, location: str | None = None,
    attendees: list[str] | None = None, timezone: str | None = None,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Create a calendar event. start/end are ISO datetimes e.g. "2026-09-28T14:00:00+08:00" (all-day: use "2026-09-28" dates). attendees = list of email addresses."""
    email = await resolve_email(email)
    s: dict[str, Any] = {"dateTime": start} if "T" in start else {"date": start}
    e: dict[str, Any] = {"dateTime": end} if "T" in end else {"date": end}
    if timezone:
        s["timeZone"] = timezone
        e["timeZone"] = timezone
    body: dict[str, Any] = {"summary": summary, "start": s, "end": e}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    result = await gapi_call(email, "calendar", f"/calendars/{calendar_id}/events", method="POST", body=body)
    if not result["ok"]:
        return err(result, email)
    ev = result["data"]
    return ok(email, event={"id": ev.get("id"), "summary": ev.get("summary"), "htmlLink": ev.get("htmlLink"), "attendees": [a.get("email") for a in ev.get("attendees", [])]})


@mcp.tool()
async def calendar_update(
    event_id: str, email: str | None = None,
    summary: str | None = None, start: str | None = None, end: str | None = None,
    description: str | None = None, location: str | None = None,
    attendees: list[str] | None = None, calendar_id: str = "primary",
) -> dict[str, Any]:
    """Partially update a calendar event (only provided fields change)."""
    email = await resolve_email(email)
    body: dict[str, Any] = {}
    if summary:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start:
        body["start"] = {"dateTime": start} if "T" in start else {"date": start}
    if end:
        body["end"] = {"dateTime": end} if "T" in end else {"date": end}
    if attendees is not None:
        body["attendees"] = [{"email": a} for a in attendees]
    result = await gapi_call(email, "calendar", f"/calendars/{calendar_id}/events/{event_id}", method="PATCH", body=body)
    if not result["ok"]:
        return err(result, email)
    return ok(email, event={"id": result["data"].get("id"), "summary": result["data"].get("summary"), "htmlLink": result["data"].get("htmlLink")})


@mcp.tool()
async def calendar_delete(event_id: str, email: str | None = None, calendar_id: str = "primary") -> dict[str, Any]:
    """Delete a calendar event by id."""
    email = await resolve_email(email)
    result = await gapi_call(email, "calendar", f"/calendars/{calendar_id}/events/{event_id}", method="DELETE")
    if not result["ok"]:
        return err(result, email)
    return ok(email, deleted=event_id)


@mcp.tool()
async def calendar_calendars_list(email: str | None = None) -> dict[str, Any]:
    """List all calendars the account can access (ids for calendar_events/calendar_create)."""
    email = await resolve_email(email)
    result = await gapi_call(email, "calendar", "/users/me/calendarList")
    if not result["ok"]:
        return err(result, email)
    return ok(email, calendars=[{"id": c.get("id"), "summary": c.get("summary"), "primary": c.get("primary", False)} for c in result["data"].get("items", [])])


@mcp.tool()
async def calendar_freebusy(
    time_min: str, time_max: str, email: str | None = None,
    calendar_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Query free/busy for one or more calendars between ISO datetimes. Returns busy intervals — useful before scheduling."""
    email = await resolve_email(email)
    result = await gapi_call(
        email, "calendar", "/freeBusy", method="POST",
        body={"timeMin": time_min, "timeMax": time_max, "items": [{"id": c} for c in (calendar_ids or ["primary"])]},
    )
    if not result["ok"]:
        return err(result, email)
    out = {}
    for cal, info in (result["data"].get("calendars") or {}).items():
        out[cal] = {"busy": info.get("busy", [])}
    return ok(email, freebusy=out)
