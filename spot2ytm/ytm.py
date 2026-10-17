"""YouTube Music access through the unofficial ``ytmusicapi`` library.

Authentication uses request headers copied from a logged-in browser session
(the "browser" method of ytmusicapi), which needs no Google Cloud project.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicError

from .config import write_private
from .models import Candidate

ADD_BATCH_SIZE = 50


class YTMError(Exception):
    pass


def parse_result(raw: dict) -> Candidate | None:
    if not raw.get("videoId"):
        return None
    return Candidate(
        video_id=raw["videoId"],
        title=raw.get("title") or "",
        artists=[a["name"] for a in raw.get("artists") or [] if a.get("name")],
        album=(raw.get("album") or {}).get("name") or "",
        duration_s=raw.get("duration_seconds"),
        result_type="video" if raw.get("resultType") == "video" else "song",
    )


class YTMusicClient:
    def __init__(self, auth_file: Path):
        self.auth_file = auth_file
        # ytmusicapi shares one requests session; the worker thread and the
        # web handlers (manual search) must not use it at the same time.
        self._lock = threading.RLock()
        self._api: YTMusic | None = None
        self._account: dict | None = None
        self._anonymous: YTMusic | None = None  # good enough for searching
        if auth_file.is_file():
            try:
                self._api = YTMusic(str(auth_file))
            except Exception:  # corrupted file: treat as disconnected
                self._api = None

    @property
    def connected(self) -> bool:
        return self._api is not None

    def connect_with_headers(self, headers_raw: str) -> dict:
        """Validate pasted browser headers and store them. Returns account info."""
        headers_raw = headers_raw.strip()
        if not headers_raw:
            raise YTMError("Paste the request headers first.")
        if headers_raw.lstrip().startswith("{"):
            # Already a ytmusicapi browser.json
            try:
                headers_json = json.dumps(json.loads(headers_raw))
            except json.JSONDecodeError as exc:
                raise YTMError(f"That looks like JSON but could not be parsed: {exc}") from exc
        else:
            try:
                headers_json = setup(headers_raw=headers_raw)
            except YTMusicError as exc:
                raise YTMError(str(exc)) from exc
        try:
            api = YTMusic(headers_json)
            account = api.get_account_info()
        except Exception as exc:
            raise YTMError(
                "YouTube Music rejected these headers. Make sure you copied them from a "
                f"request made while logged in to music.youtube.com. ({exc})"
            ) from exc
        write_private(self.auth_file, headers_json)
        with self._lock:
            self._api = api
            self._account = account
        return account

    def disconnect(self) -> None:
        with self._lock:
            self._api = None
            self._account = None
        self.auth_file.unlink(missing_ok=True)

    def account(self) -> dict | None:
        if not self._api:
            return None
        if self._account is None:
            with self._lock:
                try:
                    self._account = self._api.get_account_info()
                except Exception:
                    return {"accountName": "(could not load account)"}
        return self._account

    def _anonymous_api(self) -> YTMusic:
        if self._anonymous is None:
            self._anonymous = YTMusic()
        return self._anonymous

    def _authed(self) -> YTMusic:
        if not self._api:
            raise YTMError("YouTube Music is not connected")
        return self._api

    def search(self, query: str, filter: str = "songs", limit: int = 10) -> list[Candidate]:
        api = self._api or self._anonymous_api()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with self._lock:
                    raw = api.search(query, filter=filter, limit=limit)
                break
            except Exception as exc:  # network hiccups, throttling
                last_exc = exc
                time.sleep(2**attempt)
        else:
            raise YTMError(f"Search failed for {query!r}: {last_exc}")
        results = []
        for item in raw:
            if item.get("resultType") not in ("song", "video"):
                continue
            cand = parse_result(item)
            if cand:
                results.append(cand)
        return results[:limit]

    def playlist_exists(self, playlist_id: str) -> bool:
        try:
            with self._lock:
                self._authed().get_playlist(playlist_id, limit=1)
            return True
        except YTMError:
            raise
        except Exception:
            return False

    def playlist_video_ids(self, playlist_id: str) -> set[str]:
        with self._lock:
            data = self._authed().get_playlist(playlist_id, limit=None)
        return {t["videoId"] for t in data.get("tracks") or [] if t.get("videoId")}

    def create_playlist(self, title: str, description: str, privacy: str) -> str:
        title = title.replace("<", "").replace(">", "").strip() or "Spotify playlist"
        with self._lock:
            result = self._authed().create_playlist(title, description[:5000], privacy_status=privacy)
        if not isinstance(result, str):
            raise YTMError(f"Could not create the playlist: {result}")
        return result

    def add_items(self, playlist_id: str, video_ids: list[str]) -> None:
        """Add videos in batches. ``duplicates=True`` makes YouTube skip
        duplicates instead of rejecting the whole batch."""
        for start in range(0, len(video_ids), ADD_BATCH_SIZE):
            batch = video_ids[start : start + ADD_BATCH_SIZE]
            with self._lock:
                result = self._authed().add_playlist_items(playlist_id, batch, duplicates=True)
            if not isinstance(result, dict) or "SUCCEEDED" not in str(result.get("status", "")):
                raise YTMError(f"Adding songs failed: {str(result)[:300]}")
            time.sleep(0.5)

    @staticmethod
    def playlist_url(playlist_id: str) -> str:
        return f"https://music.youtube.com/playlist?list={playlist_id}"
