"""Minimal Spotify Web API client.

Uses the Authorization Code flow with PKCE, so only a client id is needed (no
client secret to keep around). Written against the API as it stands after the
February 2026 Development Mode changes (``/playlists/{id}/items``, ``item``
instead of ``track`` in playlist entries), with fallbacks for the old names.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

import httpx

from .config import write_private
from .models import Playlist, Track

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"

LIKED_SONGS_ID = "liked"

log = logging.getLogger(__name__)


class SpotifyError(Exception):
    pass


class SpotifyAuthError(SpotifyError):
    """Not connected, or the refresh token was revoked."""


class SpotifyPremiumError(SpotifyError):
    """Spotify refuses developer apps whose owner is not on Premium."""


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def clean_description(text: str | None) -> str:
    """Spotify descriptions are HTML-escaped and may contain links."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class SpotifyClient:
    def __init__(self, client_id: str, token_file: Path, transport: httpx.BaseTransport | None = None):
        self.client_id = client_id
        self.token_file = token_file
        self._http = httpx.Client(transport=transport, timeout=30)
        self._lock = threading.Lock()
        self._token: dict | None = self._read_token()
        self._me: dict | None = None

    # -- auth -----------------------------------------------------------------

    def _read_token(self) -> dict | None:
        try:
            return json.loads(self.token_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _store_token(self, payload: dict) -> None:
        token = {
            "access_token": payload["access_token"],
            # Spotify may or may not rotate the refresh token on refresh.
            "refresh_token": payload.get("refresh_token") or (self._token or {}).get("refresh_token"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)) - 60,
            "scope": payload.get("scope", ""),
        }
        self._token = token
        write_private(self.token_file, json.dumps(token))

    @property
    def connected(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> None:
        resp = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "code_verifier": code_verifier,
            },
        )
        if resp.status_code != 200:
            raise SpotifyAuthError(f"Token exchange failed: {resp.text}")
        self._me = None
        self._store_token(resp.json())

    def disconnect(self) -> None:
        self._token = None
        self._me = None
        self.token_file.unlink(missing_ok=True)

    def _access_token(self) -> str:
        with self._lock:
            if not self.connected:
                raise SpotifyAuthError("Spotify is not connected")
            assert self._token is not None
            if time.time() >= self._token.get("expires_at", 0):
                resp = self._http.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._token["refresh_token"],
                        "client_id": self.client_id,
                    },
                )
                if resp.status_code != 200:
                    raise SpotifyAuthError(f"Could not refresh the Spotify token: {resp.text}")
                self._store_token(resp.json())
            return self._token["access_token"]

    # -- requests -------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> dict:
        if not url.startswith("http"):
            url = API_URL + url
        for attempt in range(5):
            resp = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {self._access_token()}"}
            )
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", "2")), 30)
                log.warning("Spotify rate limit, waiting", extra={"url": url, "waitSeconds": wait})
                time.sleep(wait)
                continue
            if resp.status_code == 401 and attempt == 0:
                # Token revoked or clock skew: force a refresh once.
                if self._token:
                    self._token["expires_at"] = 0
                continue
            if resp.status_code >= 500 and attempt < 4:
                log.warning("Spotify server error, retrying", extra={"url": url, "statusCode": resp.status_code})
                time.sleep(1 + attempt)
                continue
            if resp.status_code == 403 and "premium" in resp.text.lower():
                raise SpotifyPremiumError(
                    "Spotify refuses this developer app because its owner has no active Premium subscription."
                )
            if resp.status_code >= 400:
                raise SpotifyError(f"GET {url} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise SpotifyError(f"GET {url} kept failing")

    def _paginate(self, url: str, params: dict | None = None) -> Iterator[dict]:
        page = self._get(url, params)
        while True:
            yield from page.get("items") or []
            if not page.get("next"):
                return
            page = self._get(page["next"])

    # -- API ------------------------------------------------------------------

    def me(self) -> dict:
        if self._me is None:
            self._me = self._get("/me")
        return self._me

    def playlists(self) -> list[Playlist]:
        me_id = self.me()["id"]
        result = []
        for raw in self._paginate("/me/playlists", {"limit": 50}):
            if raw:
                result.append(parse_playlist(raw, me_id))
        return result

    def liked_songs_count(self) -> int:
        return int(self._get("/me/tracks", {"limit": 1}).get("total", 0))

    def liked_songs_playlist(self) -> Playlist:
        me = self.me()
        return Playlist(
            id=LIKED_SONGS_ID,
            name="Liked Songs",
            owner_id=me["id"],
            owner_name=me.get("display_name") or me["id"],
            total=self.liked_songs_count(),
            description="Your Liked Songs on Spotify",
        )

    def playlist(self, playlist_id: str) -> Playlist:
        if playlist_id == LIKED_SONGS_ID:
            return self.liked_songs_playlist()
        raw = self._get(f"/playlists/{playlist_id}", {"fields": "id,name,description,owner,collaborative,images,items(total),tracks(total)"})
        return parse_playlist(raw, self.me()["id"])

    def playlist_tracks(self, playlist_id: str) -> list[Track]:
        if playlist_id == LIKED_SONGS_ID:
            entries = self._paginate("/me/tracks", {"limit": 50})
        else:
            try:
                entries = list(self._paginate(f"/playlists/{playlist_id}/items", {"limit": 50}))
            except SpotifyError as exc:
                if "-> 404" not in str(exc):
                    raise
                entries = list(self._paginate(f"/playlists/{playlist_id}/tracks", {"limit": 50}))
        tracks = []
        for entry in entries:
            track = parse_track_entry(entry)
            if track:
                tracks.append(track)
        return tracks


def parse_playlist(raw: dict, me_id: str) -> Playlist:
    owner = raw.get("owner") or {}
    counts = raw.get("items") if isinstance(raw.get("items"), dict) else raw.get("tracks") or {}
    images = raw.get("images") or []
    owner_id = owner.get("id", "")
    collaborative = bool(raw.get("collaborative"))
    return Playlist(
        id=raw["id"],
        name=raw.get("name") or "(untitled)",
        owner_id=owner_id,
        owner_name=owner.get("display_name") or owner_id,
        total=int((counts or {}).get("total") or 0),
        description=clean_description(raw.get("description")),
        image_url=images[-1]["url"] if images else None,
        collaborative=collaborative,
        readable=owner_id == me_id or collaborative,
    )


def parse_track_entry(entry: dict) -> Track | None:
    """Turn a playlist/library entry into a Track; skip podcasts and gaps."""
    item = entry.get("item") or entry.get("track")
    if not item or item.get("type", "track") != "track":
        return None
    artists = [a.get("name") for a in item.get("artists") or [] if a.get("name")]
    if not item.get("name"):
        return None
    return Track(
        title=item["name"],
        artists=artists,
        album=(item.get("album") or {}).get("name") or "",
        duration_ms=int(item.get("duration_ms") or 0),
        spotify_id=None if entry.get("is_local") or item.get("is_local") else item.get("id"),
        isrc=(item.get("external_ids") or {}).get("isrc"),
    )
