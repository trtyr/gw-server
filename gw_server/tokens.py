"""OAuth token management — file-compatible with the google-workspace node skill.

Token files live at <tokens_dir>/<urlencoded-email>.json with fields:
  access_token, refresh_token, scope, token_type, expiry_date (ms epoch),
  __authMode ("local"|"cloud"), __email.

Refresh paths (mirrors common.js authorize()):
  cloud: POST {cloud_fn_url}/refreshToken {"refresh_token": ...} (no client secret needed)
  local: POST https://oauth2.googleapis.com/token with client_id/secret from credentials.json

Refreshed tokens are written back (chmod 600) so the node CLI and this server
can share the same token store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import config

log = logging.getLogger("gw.tokens")

CLOUD_FN_URL = config.cloud_fn_url

# service -> (root, path-prefix template). Aligned with Google REST endpoints.
SERVICE_ROUTES: dict[str, tuple[str, str]] = {
    "gmail": ("https://gmail.googleapis.com", "/gmail/{version}"),
    "drive": ("https://www.googleapis.com", "/drive/{version}"),
    "calendar": ("https://www.googleapis.com", "/calendar/{version}"),
    "tasks": ("https://tasks.googleapis.com", "/tasks/{version}"),
    "oauth2": ("https://www.googleapis.com", "/oauth2/{version}"),
    "admin": ("https://admin.googleapis.com", "/{version}"),
    "docs": ("https://docs.googleapis.com", "/{version}"),
    "sheets": ("https://sheets.googleapis.com", "/{version}"),
    "slides": ("https://slides.googleapis.com", "/{version}"),
    "people": ("https://people.googleapis.com", "/{version}"),
    "chat": ("https://chat.googleapis.com", "/{version}"),
}

DEFAULT_VERSIONS: dict[str, str] = {
    "gmail": "v1",
    "drive": "v3",
    "calendar": "v3",
    "tasks": "v1",
    "oauth2": "v2",
    "admin": "v1",
    "docs": "v1",
    "sheets": "v4",
    "slides": "v1",
    "people": "v1",
    "chat": "v1",
}

# Mirrors encodeURIComponent so filenames match the node skill exactly.
_FILENAME_SAFE = "-_.!~*'()"

EXPIRY_SKEW_MS = 60_000  # refresh when expiring within 60s (same as common.js)


class TokenError(Exception):
    """Token store problem (client-actionable)."""


class TokenNotFound(TokenError):
    pass


def normalize_email(email: str) -> str:
    normalized = str(email or "").strip().lower()
    if not normalized:
        raise TokenError("email is required")
    if "@" not in normalized or " " in normalized:
        raise TokenError(f"invalid email address: {email}")
    return normalized


def token_filename(email: str) -> str:
    return urllib.parse.quote(normalize_email(email), safe=_FILENAME_SAFE) + ".json"


def build_url(
    service: str,
    path: str,
    version: str | None = None,
) -> str:
    """service+path -> full REST URL. Raises TokenError on unknown service."""
    service = service.strip().lower()
    route = SERVICE_ROUTES.get(service)
    if route is None:
        raise TokenError(
            f"unknown service '{service}'. known: {', '.join(sorted(SERVICE_ROUTES))}"
        )
    root, prefix_tmpl = route
    ver = version or DEFAULT_VERSIONS.get(service, "v1")
    prefix = prefix_tmpl.format(version=ver)
    if not path.startswith("/"):
        path = "/" + path
    return root + prefix + path


class TokenManager:
    def __init__(self, tokens_dir: Path, forced_mode: str | None = None) -> None:
        self.tokens_dir = Path(tokens_dir).expanduser()
        self.forced_mode = forced_mode
        self._locks: dict[str, asyncio.Lock] = {}

    # -- locking -----------------------------------------------------------

    def _lock(self, email: str) -> asyncio.Lock:
        if email not in self._locks:
            self._locks[email] = asyncio.Lock()
        return self._locks[email]

    # -- file io ------------------------------------------------------------

    def _path(self, email: str) -> Path:
        return self.tokens_dir / token_filename(email)

    def load(self, email: str) -> dict[str, Any] | None:
        path = self._path(email)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise TokenError(f"failed to read token file for {email}: {exc}") from exc

    def save(self, email: str, token: dict[str, Any]) -> None:
        email = normalize_email(email)
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            **token,
            "__authMode": self.resolve_mode(token),
            "__email": email,
        }
        path = self._path(email)
        path.write_text(json.dumps(payload, indent=2), "utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        except OSError:
            pass

    # -- mode resolution ----------------------------------------------------

    def resolve_mode(self, token: dict[str, Any] | None) -> str:
        if self.forced_mode:
            return self.forced_mode
        if token and token.get("__authMode") in ("local", "cloud"):
            return token["__authMode"]
        if config.credentials_path.is_file():
            return "local"
        return "cloud"

    # -- account enumeration (no secret values) ------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        if not self.tokens_dir.is_dir():
            return []
        now_ms = int(time.time() * 1000)
        accounts: list[dict[str, Any]] = []
        for path in sorted(self.tokens_dir.glob("*.json")):
            try:
                token = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            email = token.get("__email") or urllib.parse.unquote(path.stem)
            expiry = token.get("expiry_date")
            scopes = token.get("scope") or ""
            accounts.append(
                {
                    "email": normalize_email(email),
                    "authMode": self.resolve_mode(token),
                    "scopes": scopes.split() if isinstance(scopes, str) else scopes,
                    "expiryDate": expiry,
                    "expired": expiry < now_ms if isinstance(expiry, int) else None,
                }
            )
        accounts.sort(key=lambda a: a["email"])
        return accounts

    # -- access token with refresh -------------------------------------------

    async def access_token(self, email: str) -> str:
        email = normalize_email(email)
        async with self._lock(email):
            token = self.load(email)
            if token is None:
                raise TokenNotFound(
                    f"no token for {email}; login first on the skill host and "
                    "copy tokens to the server"
                )

            access = token.get("access_token")
            expiry = token.get("expiry_date") or 0
            if access and expiry > time.time() * 1000 + EXPIRY_SKEW_MS:
                return access

            if not token.get("refresh_token"):
                raise TokenError(
                    f"token for {email} is expired and has no refresh_token; re-login required"
                )

            refreshed = await self._refresh(email, token)
            self.save(email, refreshed)
            log.info("token refreshed for %s (%s)", email, self.resolve_mode(refreshed))
            return refreshed["access_token"]

    async def _refresh(self, email: str, token: dict[str, Any]) -> dict[str, Any]:
        mode = self.resolve_mode(token)
        refresh_token = token["refresh_token"]

        async with httpx.AsyncClient(timeout=20) as client:
            if mode == "cloud":
                try:
                    resp = await client.post(
                        f"{CLOUD_FN_URL}/refreshToken",
                        json={"refresh_token": refresh_token},
                    )
                except httpx.HTTPError as exc:
                    raise TokenError(f"cloud token refresh failed: {exc}") from exc
                if resp.status_code >= 400:
                    raise TokenError(
                        f"cloud token refresh failed: {resp.status_code} {resp.text[:200]}"
                    )
                merged = dict(resp.json())
            else:
                creds = self._load_credentials()
                try:
                    resp = await client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": creds["client_id"],
                            "client_secret": creds["client_secret"],
                            "refresh_token": refresh_token,
                            "grant_type": "refresh_token",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise TokenError(f"local token refresh failed: {exc}") from exc
                if resp.status_code >= 400:
                    raise TokenError(
                        f"local token refresh failed: {resp.status_code} {resp.text[:200]}"
                    )
                body = resp.json()
                merged = {
                    "access_token": body["access_token"],
                    "token_type": body.get("token_type", "Bearer"),
                    "scope": body.get("scope", token.get("scope")),
                    "expiry_date": int(time.time() * 1000)
                    + int(body.get("expires_in", 3600)) * 1000,
                }

        if not merged.get("access_token"):
            raise TokenError(f"refresh for {email} returned no access_token")
        merged.setdefault(
            "expiry_date", int(time.time() * 1000) + 3300 * 1000
        )
        merged["refresh_token"] = refresh_token  # google may omit it; keep ours
        merged["scope"] = merged.get("scope") or token.get("scope")
        merged["token_type"] = merged.get("token_type") or token.get("token_type", "Bearer")
        return merged

    def _load_credentials(self) -> dict[str, Any]:
        if not config.credentials_path.is_file():
            raise TokenError(
                f"local auth mode requires credentials.json at {config.credentials_path}"
            )
        raw = json.loads(config.credentials_path.read_text("utf-8"))
        creds = raw.get("installed") or raw.get("web")
        if not creds or not creds.get("client_id") or not creds.get("client_secret"):
            raise TokenError("invalid credentials.json structure")
        return creds
