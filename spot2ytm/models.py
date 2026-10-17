from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Track:
    """A track as read from Spotify, the input of the matcher."""

    title: str
    artists: list[str]
    album: str = ""
    duration_ms: int = 0
    spotify_id: str | None = None  # None for local files
    isrc: str | None = None

    @property
    def cache_key(self) -> str:
        if self.spotify_id:
            return self.spotify_id
        return "local:" + "|".join([", ".join(self.artists), self.title]).lower()


@dataclass
class Candidate:
    """A YouTube Music search result, scored against a Track."""

    video_id: str
    title: str
    artists: list[str]
    album: str = ""
    duration_s: int | None = None
    result_type: str = "song"  # "song" or "video"
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "duration_s": self.duration_s,
            "result_type": self.result_type,
            "score": round(self.score, 3),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(
            video_id=d["video_id"],
            title=d["title"],
            artists=list(d.get("artists") or []),
            album=d.get("album") or "",
            duration_s=d.get("duration_s"),
            result_type=d.get("result_type") or "song",
            score=float(d.get("score") or 0.0),
        )


@dataclass
class Playlist:
    id: str
    name: str
    owner_id: str
    owner_name: str
    total: int
    description: str = ""
    image_url: str | None = None
    collaborative: bool = False
    # Since March 2026 Spotify only returns the items of playlists you own or
    # collaborate on; followed playlists only come with metadata.
    readable: bool = True
    extra: dict = field(default_factory=dict)
