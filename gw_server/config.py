"""gw-server configuration (env-driven, fail-closed on missing GW_TOKEN)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Mirrors google-workspace skill common.js (cloud auth mode).
DEFAULT_CLOUD_FUNCTION_URL = "https://google-workspace-extension.geminicli.com"

CONFIG_DIR = Path(
    os.environ.get("GOOGLE_WORKSPACE_CONFIG_DIR")
    or Path.home() / ".pi" / "google-workspace"
)


@dataclass(frozen=True)
class Config:
    gw_token: str
    tokens_dir: Path
    credentials_path: Path
    auth_mode: str | None  # "local" | "cloud" | None (auto per token file)
    host: str
    port: int
    cloud_fn_url: str
    public_url: str

    @property
    def oauth_client(self) -> tuple[str, str] | None:
        """Own OAuth client (id, secret) for the standard code flow, if configured.

        Sources (first wins): GW_CLIENT_ID/GW_CLIENT_SECRET env, credentials.json
        (installed/web — same file the node skill used for local mode).
        """
        cid = os.environ.get("GW_CLIENT_ID", "").strip()
        csec = os.environ.get("GW_CLIENT_SECRET", "").strip()
        if cid and csec:
            return cid, csec
        try:
            raw = json.loads(self.credentials_path.read_text("utf-8"))
            creds = raw.get("installed") or raw.get("web") or {}
            if creds.get("client_id") and creds.get("client_secret"):
                return creds["client_id"], creds["client_secret"]
        except (OSError, ValueError):
            pass
        return None

    @classmethod
    def from_env(cls) -> "Config":
        gw_token = os.environ.get("GW_TOKEN", "").strip()
        if not gw_token:
            raise SystemExit(
                "gw-server: GW_TOKEN is not set. "
                "Generate one (e.g. `openssl rand -hex 32`) and export GW_TOKEN."
            )

        mode = os.environ.get("GW_AUTH_MODE", "").strip().lower()
        if mode not in ("", "local", "cloud"):
            raise SystemExit("gw-server: GW_AUTH_MODE must be '', 'local' or 'cloud'.")

        return cls(
            gw_token=gw_token,
            tokens_dir=Path(
                os.environ.get("GOOGLE_WORKSPACE_TOKENS_DIR")
                or CONFIG_DIR / "tokens"
            ),
            credentials_path=Path(
                os.environ.get("GOOGLE_WORKSPACE_CREDENTIALS")
                or CONFIG_DIR / "credentials.json"
            ),
            auth_mode=mode or None,
            host=os.environ.get("GW_HOST", "127.0.0.1"),
            port=int(os.environ.get("GW_PORT", "8787")),
            cloud_fn_url=os.environ.get(
                "GW_CLOUD_FN_URL", DEFAULT_CLOUD_FUNCTION_URL
            ).rstrip("/"),
            # Public base URL for OAuth callbacks. On a headless server set this
            # to the public origin (e.g. https://gw.trtyr.top) so the user can
            # complete login from any browser; locally localhost works.
            public_url=os.environ.get("GW_PUBLIC_URL", "").rstrip("/")
            or f"http://localhost:{os.environ.get('GW_PORT', '8787')}",
        )


config = Config.from_env()
