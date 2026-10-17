"""YouTube Music access through the unofficial ``ytmusicapi`` library.

Authentication uses a logged-in browser session (the "browser" method of
ytmusicapi), which needs no Google Cloud project: either request headers pasted
by the user, or the YouTube cookies read from a browser on this machine.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicError

from .config import write_private
from .models import Candidate

ADD_BATCH_SIZE = 50
# Browsers whose cookies can be read (by yt-dlp's cookie extractor).
BROWSERS = {
    "edge": "Microsoft Edge", "chrome": "Google Chrome", "firefox": "Firefox", "brave": "Brave",
    "chromium": "Chromium", "opera": "Opera", "vivaldi": "Vivaldi",
}

log = logging.getLogger(__name__)


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


class _CookieLogger:
    """Quiet logger for yt-dlp's cookie extractor that counts decryption failures."""

    def __init__(self) -> None:
        self.decrypt_failures = 0

    def debug(self, message: str, *args, **kwargs) -> None:
        log.debug("%s", message)

    info = debug

    def warning(self, message: str, *args, **kwargs) -> None:
        if "failed to decrypt" in message:
            self.decrypt_failures += 1
        else:
            log.warning("%s", message)

    def error(self, message: str, *args, **kwargs) -> None:
        log.error("%s", message)


def _keyring_secrets(labels: list[str]) -> list[bytes]:
    """Secrets of the GNOME keyring items with one of these labels."""
    try:
        import secretstorage
    except ImportError:
        return []
    try:
        with closing(secretstorage.dbus_init()) as conn:
            collection = secretstorage.get_default_collection(conn)
            return [item.get_secret() for item in collection.get_all_items() if item.get_label() in labels]
    except Exception as exc:  # no keyring service, locked collection...
        log.debug("keyring not available: %s", exc)
        return []


@contextmanager
def _keyring_secret(secret: bytes | None):
    """Make yt-dlp decrypt with this secret instead of looking it up itself."""
    import yt_dlp.cookies as ytc

    if secret is None:
        yield
        return
    original = ytc._get_gnome_keyring_password
    ytc._get_gnome_keyring_password = lambda browser_keyring_name, logger: secret
    try:
        yield
    finally:
        ytc._get_gnome_keyring_password = original


def _candidate_secrets(browser: str) -> list[bytes | None]:
    """Keys to try for a Chromium-based browser on Linux.

    yt-dlp takes the first keyring item named "<browser> Safe Storage", but
    several apps can own one with the same name (VS Code and other Electron
    apps also create "Chromium Safe Storage", and Edge uses that name too), so
    try each of them until one decrypts the cookies.
    """
    import yt_dlp.cookies as ytc

    if not sys.platform.startswith("linux") or browser not in ytc.CHROMIUM_BASED_BROWSERS:
        return [None]
    name = ytc._get_chromium_based_browser_settings(browser)["keyring_name"]
    labels = list(dict.fromkeys([f"{name} Safe Storage", f"{BROWSERS[browser]} Safe Storage"]))
    return [None, *_keyring_secrets(labels)]


def browser_cookies(browser: str) -> dict[str, str]:
    """YouTube cookies of a browser installed on this machine."""
    from yt_dlp.cookies import extract_cookies_from_browser

    cookies: dict[str, str] = {}
    failures = 0
    for secret in _candidate_secrets(browser):
        logger = _CookieLogger()
        with _keyring_secret(secret):
            jar = extract_cookies_from_browser(browser, logger=logger)
        cookies = {c.name: c.value for c in jar if c.domain.lstrip(".").endswith("youtube.com") and c.value}
        failures = logger.decrypt_failures
        if not failures:
            return cookies
    if failures and "SAPISID" not in cookies and "__Secure-3PAPISID" not in cookies:
        raise YTMError(
            f"the browser's cookies are encrypted with a key from the system keyring that could not be used "
            f"({failures} cookies could not be decrypted)"
        )
    return cookies


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
        log.info("YouTube Music connected", extra={"account": account.get("accountName")})
        return account

    def connect_from_browser(self, browser: str, read_cookies=browser_cookies) -> dict:
        """Log in with the YouTube cookies of a browser on this machine."""
        name = BROWSERS.get(browser)
        if name is None:
            raise YTMError("Unknown browser.")
        try:
            cookies = read_cookies(browser)
        except Exception as exc:
            raise YTMError(
                f"Could not read the cookies of {name}: {exc}. Try another browser, or paste the headers by hand."
            ) from exc
        if "SAPISID" not in cookies and "__Secure-3PAPISID" not in cookies:
            raise YTMError(
                f"No YouTube login found in {name}. Open music.youtube.com there, log in, and try again."
            )
        # ytmusicapi computes the real authorization header from the SAPISID
        # cookie on every request; it only needs one to be present here.
        headers = [
            "cookie: " + "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "x-goog-authuser: 0",
            "authorization: SAPISIDHASH placeholder",
            "origin: https://music.youtube.com",
        ]
        return self.connect_with_headers("\n".join(headers))

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
                log.warning("YouTube Music search failed, retrying",
                            extra={"query": query, "filter": filter, "attempt": attempt + 1, "error": str(exc)})
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
        log.info("playlist created on YouTube Music", extra={"title": title, "privacy": privacy, "ytPlaylistId": result})
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
            log.debug("songs added to playlist", extra={"ytPlaylistId": playlist_id, "count": len(batch)})
            time.sleep(0.5)

    @staticmethod
    def playlist_url(playlist_id: str) -> str:
        return f"https://music.youtube.com/playlist?list={playlist_id}"
