"""Read Spotify without an account: public embed pages, no login, no token.

Spotify only lets developer apps use the Web API when their owner has Premium,
so Free users import through the pages behind Spotify's embeddable players
(``open.spotify.com/embed/...``). Those pages carry the data as JSON:

* a playlist or album page lists its first 100 songs (title, artists, length);
* a track page describes one song, so a pasted list of track links of any
  length can be read one song at a time.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from .models import Playlist, Track

EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
EMBED_TRACK_LIMIT = 100  # embed pages never list more songs than this
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

_ID = r"([A-Za-z0-9]{22})"
_LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)(playlist|album|track)[/:]" + _ID)
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


class PublicSpotifyError(Exception):
    pass


def parse_link(text: str) -> tuple[str, str] | None:
    """('playlist' | 'album' | 'track', id) for a Spotify link or URI."""
    m = _LINK.search(text.strip())
    return (m.group(1), m.group(2)) if m else None


def track_ids(text: str) -> list[str]:
    """Track ids in pasted text, in order (duplicates kept, like a playlist).

    Selecting songs in the Spotify app and pressing Ctrl+C copies one
    ``https://open.spotify.com/track/...`` link per line.
    """
    return [m.group(2) for m in _LINK.finditer(text) if m.group(1) == "track"]


def split_artists(subtitle: str) -> list[str]:
    # Embed pages join artists with ",<no-break space>"; a plain ", " belongs
    # to a name ("Tyler, The Creator").
    return [a.strip() for a in subtitle.split(", ") if a.strip()]


class PublicSpotify:
    def __init__(self, transport: httpx.BaseTransport | None = None, delay: float = 0.1):
        self.delay = delay
        self._http = httpx.Client(
            timeout=20, transport=transport, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
        )

    def _entity(self, kind: str, id_: str) -> dict:
        url = EMBED_URL.format(kind=kind, id=id_)
        for attempt in range(3):
            try:
                resp = self._http.get(url)
            except httpx.HTTPError as exc:
                error = f"could not reach Spotify: {exc}"
            else:
                if resp.status_code == 404:
                    break
                if resp.status_code == 200:
                    m = _NEXT_DATA.search(resp.text)
                    try:
                        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
                    except (AttributeError, KeyError, TypeError, ValueError):
                        break  # page without data: missing, private or removed
                    if entity and entity.get("type") == kind:
                        return entity
                    break
                error = f"Spotify answered {resp.status_code}"
            time.sleep(1 + attempt * 2)
        else:
            raise PublicSpotifyError(error)
        raise PublicSpotifyError(f"This {kind} was not found. Is it public?")

    def collection(self, kind: str, id_: str) -> tuple[Playlist, list[Track]]:
        """A public playlist or album and its first 100 songs."""
        entity = self._entity(kind, id_)
        sources = (entity.get("coverArt") or {}).get("sources") or []
        name = entity.get("name") or entity.get("title") or "Untitled"
        album = name if kind == "album" else ""
        tracks = []
        for item in entity.get("trackList") or []:
            uri = item.get("uri") or ""
            if item.get("entityType", "track") != "track" or not uri.startswith("spotify:track:"):
                continue  # podcast episodes
            tracks.append(Track(
                title=item.get("title") or "",
                artists=split_artists(item.get("subtitle") or ""),
                album=album,
                duration_ms=int(item.get("duration") or 0),
                spotify_id=uri.rsplit(":", 1)[1],
            ))
        playlist = Playlist(
            id=f"public:{kind}:{id_}",
            name=name,
            owner_id="",
            owner_name=entity.get("subtitle") or "",
            total=len(tracks),
            image_url=sources[0]["url"] if sources else None,
        )
        return playlist, tracks

    def track(self, id_: str) -> Track:
        entity = self._entity("track", id_)
        if self.delay:
            time.sleep(self.delay)
        return Track(
            title=entity.get("title") or entity.get("name") or "",
            artists=[a["name"] for a in entity.get("artists") or [] if a.get("name")],
            duration_ms=int(entity.get("duration") or 0),
            spotify_id=id_,
        )
