"""SQLite storage: transfers, their tracks, the match cache and playlist links."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Candidate, Track

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_playlist_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    image_url TEXT,
    privacy TEXT NOT NULL DEFAULT 'PRIVATE',
    auto_push INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    pending_action TEXT,
    error TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    yt_playlist_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    cache_key TEXT NOT NULL,
    spotify_id TEXT,
    title TEXT NOT NULL,
    artists TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    isrc TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    video_id TEXT,
    score REAL,
    manual INTEGER NOT NULL DEFAULT 0,
    candidates TEXT NOT NULL DEFAULT '[]',
    added INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_transfer ON transfer_tracks(transfer_id, position);

CREATE TABLE IF NOT EXISTS match_cache (
    cache_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    video_id TEXT,
    score REAL,
    manual INTEGER NOT NULL DEFAULT 0,
    candidates TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL
);

-- Which YouTube Music playlist a Spotify playlist was exported to, so
-- exporting it again only adds what is missing.
CREATE TABLE IF NOT EXISTS playlist_links (
    spotify_playlist_id TEXT PRIMARY KEY,
    yt_playlist_id TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

ACTIVE_STATUSES = ("queued", "fetching", "matching", "pushing")
# Statuses a track can have. "matched" tracks are the ones sent to YouTube.
TRACK_STATUSES = ("pending", "matched", "uncertain", "not_found", "skipped")


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        with self.tx() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # -- transfers ------------------------------------------------------------

    def create_transfer(
        self, spotify_playlist_id: str, name: str, description: str, image_url: str | None,
        privacy: str, auto_push: bool,
    ) -> int:
        now = time.time()
        with self.tx() as conn:
            link = conn.execute(
                "SELECT yt_playlist_id FROM playlist_links WHERE spotify_playlist_id = ?",
                (spotify_playlist_id,),
            ).fetchone()
            cur = conn.execute(
                """INSERT INTO transfers (spotify_playlist_id, name, description, image_url, privacy,
                       auto_push, yt_playlist_id, pending_action, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'match', ?, ?)""",
                (spotify_playlist_id, name, description, image_url, privacy, int(auto_push),
                 link["yt_playlist_id"] if link else None, now, now),
            )
            return int(cur.lastrowid)

    def get_transfer(self, transfer_id: int) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()

    def list_transfers(self, limit: int = 100) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM transfers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def update_transfer(self, transfer_id: int, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as conn:
            conn.execute(f"UPDATE transfers SET {cols} WHERE id = ?", (*fields.values(), transfer_id))

    def delete_transfer(self, transfer_id: int) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))

    def unfinished_transfers(self) -> list[sqlite3.Row]:
        """Transfers that were interrupted (app restarted mid-job)."""
        return self._conn().execute(
            "SELECT * FROM transfers WHERE pending_action IS NOT NULL ORDER BY id"
        ).fetchall()

    def last_transfer_by_playlist(self) -> dict[str, sqlite3.Row]:
        rows = self._conn().execute(
            "SELECT * FROM transfers WHERE id IN (SELECT MAX(id) FROM transfers GROUP BY spotify_playlist_id)"
        ).fetchall()
        return {r["spotify_playlist_id"]: r for r in rows}

    def link_playlist(self, spotify_playlist_id: str, yt_playlist_id: str) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO playlist_links (spotify_playlist_id, yt_playlist_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(spotify_playlist_id) DO UPDATE SET
                       yt_playlist_id = excluded.yt_playlist_id, updated_at = excluded.updated_at""",
                (spotify_playlist_id, yt_playlist_id, time.time()),
            )

    # -- tracks ---------------------------------------------------------------

    def add_tracks(self, transfer_id: int, tracks: list[Track]) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM transfer_tracks WHERE transfer_id = ?", (transfer_id,))
            conn.executemany(
                """INSERT INTO transfer_tracks (transfer_id, position, cache_key, spotify_id, title,
                       artists, album, duration_ms, isrc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (transfer_id, i, t.cache_key, t.spotify_id, t.title, json.dumps(t.artists),
                     t.album, t.duration_ms, t.isrc)
                    for i, t in enumerate(tracks)
                ],
            )
            conn.execute(
                "UPDATE transfers SET total = ?, updated_at = ? WHERE id = ?",
                (len(tracks), time.time(), transfer_id),
            )

    def tracks(self, transfer_id: int, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM transfer_tracks WHERE transfer_id = ?"
        args: list = [transfer_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            args.extend(statuses)
        return self._conn().execute(sql + " ORDER BY position", args).fetchall()

    def get_track(self, track_row_id: int) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM transfer_tracks WHERE id = ?", (track_row_id,)).fetchone()

    def count_tracks(self, transfer_id: int) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM transfer_tracks WHERE transfer_id = ? GROUP BY status",
            (transfer_id,),
        ).fetchall()
        counts = {s: 0 for s in TRACK_STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        counts["added"] = self._conn().execute(
            "SELECT COUNT(*) FROM transfer_tracks WHERE transfer_id = ? AND added = 1", (transfer_id,)
        ).fetchone()[0]
        counts["to_add"] = self._conn().execute(
            "SELECT COUNT(*) FROM transfer_tracks WHERE transfer_id = ? AND status = 'matched' AND added = 0",
            (transfer_id,),
        ).fetchone()[0]
        return counts

    def set_track_match(
        self, track_row_id: int, status: str, video_id: str | None, score: float | None,
        candidates: list[Candidate] | None = None, manual: bool = False,
    ) -> None:
        sets = {"status": status, "video_id": video_id, "score": score, "manual": int(manual)}
        if candidates is not None:
            sets["candidates"] = json.dumps([c.to_dict() for c in candidates])
        cols = ", ".join(f"{k} = ?" for k in sets)
        with self.tx() as conn:
            conn.execute(f"UPDATE transfer_tracks SET {cols} WHERE id = ?", (*sets.values(), track_row_id))

    def mark_added(self, track_row_ids: list[int], added: bool = True) -> None:
        with self.tx() as conn:
            conn.executemany(
                "UPDATE transfer_tracks SET added = ? WHERE id = ?", [(int(added), i) for i in track_row_ids]
            )

    # -- match cache ----------------------------------------------------------

    def cached_match(self, cache_key: str) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM match_cache WHERE cache_key = ?", (cache_key,)).fetchone()

    def cache_match(
        self, cache_key: str, status: str, video_id: str | None, score: float | None,
        candidates: list[Candidate], manual: bool = False,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO match_cache (cache_key, status, video_id, score, manual, candidates, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET status = excluded.status,
                       video_id = excluded.video_id, score = excluded.score, manual = excluded.manual,
                       candidates = excluded.candidates, updated_at = excluded.updated_at""",
                (cache_key, status, video_id, score, int(manual),
                 json.dumps([c.to_dict() for c in candidates]), time.time()),
            )

    def forget_match(self, cache_key: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM match_cache WHERE cache_key = ?", (cache_key,))


def row_track(row: sqlite3.Row) -> Track:
    return Track(
        title=row["title"],
        artists=json.loads(row["artists"]),
        album=row["album"],
        duration_ms=row["duration_ms"],
        spotify_id=row["spotify_id"],
        isrc=row["isrc"],
    )


def row_candidates(row: sqlite3.Row) -> list[Candidate]:
    return [Candidate.from_dict(d) for d in json.loads(row["candidates"] or "[]")]
