"""Runtime settings.

Everything can be set through environment variables (or a ``.env`` file in the
working directory). The Spotify client id can also be saved from the web UI,
in which case it lives in ``<data dir>/settings.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    # Public URL the browser uses to reach the app. Spotify only accepts
    # loopback redirect URIs written as an IP (127.0.0.1), not "localhost".
    base_url: str = ""
    spotify_client_id: str = ""
    # Seconds to wait between YouTube Music searches, to stay polite.
    search_delay: float = 0.2
    allowed_hosts: list[str] = field(default_factory=list)

    @property
    def spotify_redirect_uri(self) -> str:
        return f"{self.base_url}/auth/spotify/callback"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "spot2ytm.db"

    @property
    def spotify_token_file(self) -> Path:
        return self.data_dir / "spotify_token.json"

    @property
    def ytmusic_auth_file(self) -> Path:
        return self.data_dir / "ytmusic_auth.json"

    def save_spotify_client_id(self, client_id: str) -> None:
        stored = self._read_stored()
        stored["spotify_client_id"] = client_id
        write_private(self.settings_file, json.dumps(stored, indent=2))
        self.spotify_client_id = client_id

    def _read_stored(self) -> dict:
        try:
            return json.loads(self.settings_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(Path.cwd() / ".env")
        env = os.environ
        data_dir = Path(env.get("SPOT2YTM_DATA_DIR", "data")).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        port = int(env.get("SPOT2YTM_PORT", "8000"))
        settings = cls(
            data_dir=data_dir,
            host=env.get("SPOT2YTM_HOST", "127.0.0.1"),
            port=port,
            base_url=env.get("SPOT2YTM_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/"),
            search_delay=float(env.get("SPOT2YTM_SEARCH_DELAY", "0.2")),
        )
        stored = settings._read_stored()
        settings.spotify_client_id = env.get("SPOT2YTM_SPOTIFY_CLIENT_ID") or stored.get(
            "spotify_client_id", ""
        )
        extra_hosts = [h.strip() for h in env.get("SPOT2YTM_ALLOWED_HOSTS", "").split(",") if h.strip()]
        settings.allowed_hosts = ["127.0.0.1", "localhost", *extra_hosts]
        return settings


def write_private(path: Path, content: str) -> None:
    """Write a file readable only by the current user (it holds credentials)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
