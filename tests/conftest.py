from __future__ import annotations

import pytest

from spot2ytm.config import Settings
from spot2ytm.db import Store
from spot2ytm.models import Candidate, Playlist, Track


class FakeSpotify:
    connected = True

    def __init__(self, playlists: dict[str, list[Track]] | None = None):
        self.data = playlists or {}

    def me(self):
        return {"id": "me", "display_name": "Me"}

    def playlist_tracks(self, playlist_id):
        return list(self.data[playlist_id])

    def playlist(self, playlist_id):
        return Playlist(id=playlist_id, name=f"Playlist {playlist_id}", owner_id="me", owner_name="Me",
                        total=len(self.data.get(playlist_id, [])))

    def playlists(self):
        return [self.playlist(pid) for pid in self.data]

    def liked_songs_playlist(self):
        return Playlist(id="liked", name="Liked Songs", owner_id="me", owner_name="Me", total=0)

    def disconnect(self):
        self.connected = False


class FakeYT:
    """In-memory YouTube Music: a catalogue to search and playlists to write."""

    connected = True

    def __init__(self, catalogue: list[Candidate] | None = None):
        self.catalogue = catalogue or []
        self.playlists: dict[str, list[str]] = {}
        self.searches: list[tuple[str, str]] = []

    def search(self, query, filter="songs", limit=10):
        self.searches.append((query, filter))
        words = set(query.lower().split())
        wanted = "video" if filter == "videos" else "song"
        hits = [
            Candidate(**{**c.__dict__})
            for c in self.catalogue
            if c.result_type == wanted and words & set((c.title + " " + " ".join(c.artists)).lower().split())
        ]
        return hits[:limit]

    def playlist_exists(self, playlist_id):
        return playlist_id in self.playlists

    def playlist_video_ids(self, playlist_id):
        return set(self.playlists[playlist_id])

    def create_playlist(self, title, description, privacy):
        pid = f"PL{len(self.playlists) + 1}"
        self.playlists[pid] = []
        return pid

    def add_items(self, playlist_id, video_ids):
        self.playlists[playlist_id].extend(video_ids)

    def account(self):
        return {"accountName": "Tester"}

    def disconnect(self):
        self.connected = False


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path, base_url="http://127.0.0.1:8000", search_delay=0)
    s.allowed_hosts = ["127.0.0.1", "localhost", "testserver"]
    s.spotify_client_id = "0123456789abcdef0123456789abcdef"
    return s


def song(video_id, title, artists, duration_s, album="", result_type="song"):
    return Candidate(video_id=video_id, title=title, artists=artists, album=album,
                     duration_s=duration_s, result_type=result_type)


def track(title, artists, duration_s, album="", spotify_id=None):
    return Track(title=title, artists=artists, album=album, duration_ms=duration_s * 1000,
                 spotify_id=spotify_id or title.lower().replace(" ", "_"))
