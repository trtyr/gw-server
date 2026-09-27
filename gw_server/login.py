"""Pure-Python OAuth login — replaces the node `auth.js login` flow.

Runs on a machine with a browser (e.g. the Mac):
  uv run python -m gw_server.login --email user@gmail.com

Flow (cloud mode, mirrors the node skill's interactiveLoginCloud):
  1. generate Google auth URL (public OAuth client, redirect via cloud function)
  2. open the browser, start a local callback server on a random port
  3. user consents -> cloud function exchanges the code -> 302 back to localhost
     with access_token/refresh_token/expiry_date
  4. verify identity via userinfo, store token (same file format as before)

The resulting token file is consumed directly by gw-server.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from .config import DEFAULT_CLOUD_FUNCTION_URL, config
from .tokens import TokenManager, normalize_email

DEFAULT_CLIENT_ID = "338689075775-o75k922vn5fdl18qergr96rp8g63e4d7.apps.googleusercontent.com"

IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.memberships",
    *IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/directory.readonly",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict | None = None
    error: str | None = None
    expected_csrf: str = ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/oauth2callback"):
            self.send_response(404)
            self.end_headers()
            return
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if qs.get("state") != _CallbackHandler.expected_csrf:
            _CallbackHandler.error = "OAuth state mismatch"
            self._respond(400, "State mismatch.")
            return
        if qs.get("error"):
            _CallbackHandler.error = f"Google OAuth error: {qs['error']}"
            self._respond(400, "Authentication failed.")
            return

        access = qs.get("access_token")
        expiry_raw = qs.get("expiry_date")
        if not access or not expiry_raw:
            _CallbackHandler.error = "callback did not include tokens"
            self._respond(400, "Authentication failed: missing tokens.")
            return

        _CallbackHandler.result = {
            "access_token": access,
            "refresh_token": qs.get("refresh_token"),
            "scope": qs.get("scope"),
            "token_type": qs.get("token_type", "Bearer"),
            "expiry_date": int(expiry_raw),
        }
        self._respond(200, "Authentication successful. You can close this tab.")

    def _respond(self, code: int, text: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def log_message(self, *_args) -> None:  # silence default stderr logging
        pass


def login(email: str, timeout_s: int = 300) -> None:
    email = normalize_email(email)

    server = HTTPServer(("localhost", 0), _CallbackHandler)
    port = server.server_address[1]
    csrf = secrets.token_hex(32)
    _CallbackHandler.expected_csrf = csrf

    state = base64.b64encode(
        json.dumps(
            {"uri": f"http://localhost:{port}/oauth2callback", "manual": False, "csrf": csrf}
        ).encode()
    ).decode()

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": DEFAULT_CLIENT_ID,
            "redirect_uri": DEFAULT_CLOUD_FUNCTION_URL,
            "response_type": "code",
            "access_type": "offline",
            "scope": " ".join(DEFAULT_SCOPES),
            "state": state,
            "prompt": "consent",
        }
    )

    print("ℹ️  Opening browser for Google OAuth login (cloud mode)...")
    print(f"   If the browser does not open, visit:\n   {auth_url}\n")
    webbrowser.open(auth_url)

    threading.Thread(target=server.handle_request, daemon=True).start()

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _CallbackHandler.result or _CallbackHandler.error:
            break
        time.sleep(0.3)
    server.server_close()

    if _CallbackHandler.error:
        print(f"❌ {_CallbackHandler.error}", file=sys.stderr)
        raise SystemExit(1)
    if not _CallbackHandler.result:
        print(f"❌ timed out after {timeout_s}s waiting for OAuth callback", file=sys.stderr)
        raise SystemExit(1)

    creds = _CallbackHandler.result

    # verify the signed-in identity matches the requested account
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds['access_token']}"},
        )
    if resp.status_code != 200:
        print(f"❌ identity check failed: {resp.status_code}", file=sys.stderr)
        raise SystemExit(1)
    identity_email = normalize_email(resp.json().get("email", ""))
    if identity_email != email:
        print(
            f"❌ authenticated as {identity_email}, but expected {email}. Re-run with "
            f"--email {identity_email} or sign in with the intended account.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    tokens = TokenManager(config.tokens_dir)
    tokens.save(email, creds)
    print("✅ Login successful. Token stored at:")
    print(f"   {tokens._path(email)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Workspace OAuth login (cloud mode)")
    parser.add_argument("--email", required=True, help="account to sign in, e.g. user@gmail.com")
    args = parser.parse_args()
    login(args.email)


if __name__ == "__main__":
    main()
