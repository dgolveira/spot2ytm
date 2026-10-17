"""Background worker that matches tracks and writes playlists to YouTube Music.

One worker thread processes transfers one at a time, which keeps the request
rate towards YouTube Music low. Every step is persisted, so a transfer that
was interrupted (app closed, crash) resumes where it stopped on next start.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Protocol

from . import matching
from .db import Store, row_candidates, row_track
from .models import Candidate, Playlist, Track
from .public import PublicSpotifyError

log = logging.getLogger(__name__)

MAX_CANDIDATES = 6


class SpotifySource(Protocol):
    def playlist_tracks(self, playlist_id: str) -> list[Track]: ...


class TrackSource(Protocol):
    def track(self, track_id: str) -> Track: ...


class YTMusicTarget(Protocol):
    def search(self, query: str, filter: str = "songs", limit: int = 10) -> list[Candidate]: ...
    def playlist_exists(self, playlist_id: str) -> bool: ...
    def playlist_video_ids(self, playlist_id: str) -> set[str]: ...
    def create_playlist(self, title: str, description: str, privacy: str) -> str: ...
    def add_items(self, playlist_id: str, video_ids: list[str]) -> None: ...


def find_match(yt: YTMusicTarget, track: Track, delay: float = 0.0) -> tuple[str, Candidate | None, list[Candidate]]:
    """Search YouTube Music for a track. Returns (status, best, candidates)."""
    candidates: list[Candidate] = []
    for i, (query, filter_) in enumerate(matching.search_queries(track)):
        if i and delay:
            time.sleep(delay)
        results = yt.search(query, filter_, limit=10)
        if query == track.isrc:
            results = results[:1]
        candidates = matching.rank(track, candidates + results)
        if candidates and candidates[0].score >= matching.MATCH_THRESHOLD:
            break
    candidates = candidates[:MAX_CANDIDATES]
    best = candidates[0] if candidates else None
    status = matching.status_for(best.score if best else None)
    return status, (best if status != "not_found" else None), candidates


def description_for(playlist_description: str) -> str:
    note = "Imported from Spotify with spot2ytm."
    return f"{playlist_description}\n\n{note}" if playlist_description else note


class Worker:
    def __init__(
        self, store: Store, spotify: SpotifySource, yt: YTMusicTarget, search_delay: float = 0.2,
        public: TrackSource | None = None,
    ):
        self.store = store
        self.spotify = spotify
        self.public = public
        self.yt = yt
        self.search_delay = search_delay
        self._queue: queue.Queue[int] = queue.Queue()
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- control --------------------------------------------------------------

    def start(self) -> None:
        for row in self.store.unfinished_transfers():
            self.enqueue(row["id"])
        self._thread = threading.Thread(target=self._run, name="spot2ytm-worker", daemon=True)
        self._thread.start()

    def enqueue(self, transfer_id: int) -> None:
        with self._lock:
            if transfer_id in self._queued:
                return
            self._queued.add(transfer_id)
        self._queue.put(transfer_id)

    def request(self, transfer_id: int, action: str) -> None:
        """Ask for an action ('match' or 'push') on a transfer."""
        status = "queued" if action == "match" else "pushing"
        self.store.update_transfer(transfer_id, pending_action=action, status=status, error=None)
        self.enqueue(transfer_id)

    def cancel(self, transfer_id: int) -> None:
        self.store.update_transfer(transfer_id, pending_action=None, status="cancelled")

    def retry_unresolved(self, transfer_id: int) -> None:
        """Search again for tracks that were not confidently matched."""
        rows = self.store.tracks(transfer_id, ("uncertain", "not_found"))
        for row in rows:
            if not row["manual"]:
                self.store.forget_match(row["cache_key"])
                self.store.set_track_match(row["id"], "pending", None, None, [])
        self.request(transfer_id, "match")

    def retry(self, transfer_id: int) -> None:
        transfer = self.store.get_transfer(transfer_id)
        if transfer is None:
            return
        fetched = transfer["total"] > 0
        pending = self.store.tracks(transfer_id, ("pending",))
        self.request(transfer_id, "push" if fetched and not pending else "match")

    def run_once(self, transfer_id: int) -> None:
        """Process one transfer synchronously (used by the thread and tests)."""
        try:
            self._process(transfer_id)
        except Exception as exc:  # report it in the UI instead of dying
            log.exception("transfer %s failed", transfer_id)
            self.store.update_transfer(transfer_id, status="error", error=str(exc), pending_action=None)

    def _run(self) -> None:
        while True:
            transfer_id = self._queue.get()
            with self._lock:
                self._queued.discard(transfer_id)
            self.run_once(transfer_id)

    # -- steps ----------------------------------------------------------------

    def _cancelled(self, transfer_id: int) -> bool:
        row = self.store.get_transfer(transfer_id)
        return row is None or row["pending_action"] is None

    def _process(self, transfer_id: int) -> None:
        transfer = self.store.get_transfer(transfer_id)
        if transfer is None or transfer["pending_action"] is None:
            return
        if transfer["pending_action"] == "match":
            if not self._match(transfer_id):
                return
            transfer = self.store.get_transfer(transfer_id)
            if not transfer["auto_push"]:
                self.store.update_transfer(transfer_id, status="review", pending_action=None)
                return
            self.store.update_transfer(transfer_id, pending_action="push")
        self._push(transfer_id)

    def _match(self, transfer_id: int) -> bool:
        """Returns False if the transfer was cancelled midway."""
        transfer = self.store.get_transfer(transfer_id)
        if transfer["status"] in ("queued", "fetching") and transfer["total"] == 0:
            self.store.update_transfer(transfer_id, status="fetching")
            tracks = self.spotify.playlist_tracks(transfer["spotify_playlist_id"])
            self.store.add_tracks(transfer_id, tracks)
        if not self._fill_pasted(transfer_id):
            return False

        self.store.update_transfer(transfer_id, status="matching")
        pending = self.store.tracks(transfer_id, ("pending",))
        total = self.store.get_transfer(transfer_id)["total"]
        done = total - len(pending)
        self.store.update_transfer(transfer_id, progress=done)

        for row in pending:
            if done % 10 == 0 and self._cancelled(transfer_id):
                return False
            cached = self.store.cached_match(row["cache_key"])
            if cached:
                self.store.set_track_match(
                    row["id"], cached["status"], cached["video_id"], cached["score"],
                    row_candidates(cached), manual=bool(cached["manual"]),
                )
            else:
                track = row_track(row)
                status, best, candidates = find_match(self.yt, track, self.search_delay)
                video_id = best.video_id if best else None
                score = best.score if best else (candidates[0].score if candidates else None)
                self.store.set_track_match(row["id"], status, video_id, score, candidates)
                self.store.cache_match(row["cache_key"], status, video_id, score, candidates)
                if self.search_delay:
                    time.sleep(self.search_delay)
            done += 1
            self.store.update_transfer(transfer_id, progress=done)
        return True

    def _fill_pasted(self, transfer_id: int) -> bool:
        """Look up songs that were imported as bare track links."""
        rows = [r for r in self.store.tracks(transfer_id, ("pending",)) if not r["title"]]
        if not rows:
            return True
        self.store.update_transfer(transfer_id, status="fetching", progress=0)
        for i, row in enumerate(rows):
            if i % 10 == 0 and self._cancelled(transfer_id):
                return False
            try:
                self.store.fill_track(row["id"], self.public.track(row["spotify_id"]))
            except PublicSpotifyError as exc:
                log.warning("track %s: %s", row["spotify_id"], exc)
                self.store.fill_track(row["id"], Track(title=f"Unavailable song ({row['spotify_id']})", artists=[]))
                self.store.set_track_match(row["id"], "not_found", None, None, [])
            self.store.update_transfer(transfer_id, progress=i + 1)
        return True

    def _push(self, transfer_id: int) -> None:
        transfer = self.store.get_transfer(transfer_id)
        self.store.update_transfer(transfer_id, status="pushing")
        rows = [r for r in self.store.tracks(transfer_id, ("matched",)) if not r["added"] and r["video_id"]]

        playlist_id = transfer["yt_playlist_id"]
        if playlist_id and not self.yt.playlist_exists(playlist_id):
            playlist_id = None  # deleted on YouTube Music since the last export
        if not playlist_id:
            playlist_id = self.yt.create_playlist(
                transfer["name"], description_for(transfer["description"]), transfer["privacy"]
            )
            existing: set[str] = set()
        else:
            existing = self.yt.playlist_video_ids(playlist_id)
        self.store.update_transfer(transfer_id, yt_playlist_id=playlist_id)
        self.store.link_playlist(transfer["spotify_playlist_id"], playlist_id)

        already = [r["id"] for r in rows if r["video_id"] in existing]
        if already:
            self.store.mark_added(already)

        seen = set(existing)
        batch: list = []
        for row in rows:
            if row["video_id"] in seen:
                continue
            seen.add(row["video_id"])
            batch.append(row)
        for start in range(0, len(batch), 50):
            if self._cancelled(transfer_id):
                return
            chunk = batch[start : start + 50]
            self.yt.add_items(playlist_id, [r["video_id"] for r in chunk])
            self.store.mark_added([r["id"] for r in chunk])
        # Covers songs listed twice on Spotify: only one copy goes to YouTube.
        self.store.mark_added([r["id"] for r in rows])
        self.store.update_transfer(transfer_id, status="done", pending_action=None)

    # -- manual review --------------------------------------------------------

    def choose(self, track_row_id: int, candidate: Candidate) -> None:
        row = self.store.get_track(track_row_id)
        candidates = row_candidates(row)
        if not any(c.video_id == candidate.video_id for c in candidates):
            candidates.insert(0, candidate)
        self.store.set_track_match(track_row_id, "matched", candidate.video_id, candidate.score, candidates, manual=True)
        if row["video_id"] != candidate.video_id:
            self.store.mark_added([track_row_id], added=False)
        self.store.cache_match(row["cache_key"], "matched", candidate.video_id, candidate.score, candidates, manual=True)

    def skip(self, track_row_id: int) -> None:
        row = self.store.get_track(track_row_id)
        candidates = row_candidates(row)
        self.store.set_track_match(track_row_id, "skipped", None, None, candidates, manual=True)
        self.store.cache_match(row["cache_key"], "skipped", None, None, candidates, manual=True)


def new_transfer(store: Store, playlist: Playlist, privacy: str, auto_push: bool) -> int:
    return store.create_transfer(
        playlist.id, playlist.name, playlist.description, playlist.image_url, privacy, auto_push
    )
