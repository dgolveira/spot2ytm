from spot2ytm.jobs import Worker
from spot2ytm.models import Playlist

from .conftest import FakeSpotify, FakeYT, song, track

CATALOGUE = [
    song("v-wonderwall", "Wonderwall", ["Oasis"], 259),
    song("v-champagne", "Champagne Supernova", ["Oasis"], 451),
    song("v-hurt-nin", "Hurt", ["Nine Inch Nails"], 373),
]

TRACKS = [
    track("Wonderwall - Remastered", ["Oasis"], 258),
    track("Champagne Supernova", ["Oasis"], 451),
    track("Hurt", ["Johnny Cash"], 218),  # only the NIN song exists: not found
    track("Wonderwall", ["Oasis"], 258, spotify_id="dup"),  # same song again
]


def make(store):
    spotify = FakeSpotify({"p1": TRACKS})
    yt = FakeYT(CATALOGUE)
    worker = Worker(store, spotify, yt, search_delay=0)
    pl = Playlist(id="p1", name="Britpop", owner_id="me", owner_name="Me", total=len(TRACKS))
    tid = store.create_transfer(pl.id, pl.name, "", None, "PRIVATE")
    return worker, yt, tid


def match_and_push(worker, tid):
    """Search, then confirm right away, as the user does after the review."""
    worker.run_once(tid)
    worker.request(tid, "push")
    worker.run_once(tid)


def test_full_transfer(store):
    worker, yt, tid = make(store)
    match_and_push(worker, tid)

    transfer = store.get_transfer(tid)
    assert transfer["status"] == "done"
    assert transfer["pending_action"] is None
    counts = store.count_tracks(tid)
    assert counts["matched"] == 3
    assert counts["not_found"] == 1
    # Duplicate Wonderwall is added once.
    assert yt.playlists[transfer["yt_playlist_id"]] == ["v-wonderwall", "v-champagne"]
    assert counts["to_add"] == 0


def test_review_first_then_push(store):
    worker, yt, tid = make(store)
    worker.run_once(tid)
    assert store.get_transfer(tid)["status"] == "review"
    assert yt.playlists == {}

    # Fix the missing song by hand, then push.
    hurt = store.tracks(tid, ("not_found",))[0]
    worker.choose(hurt["id"], song("v-hurt-cash", "Hurt", ["Johnny Cash"], 218))
    worker.request(tid, "push")
    worker.run_once(tid)

    transfer = store.get_transfer(tid)
    assert transfer["status"] == "done"
    assert yt.playlists[transfer["yt_playlist_id"]] == ["v-wonderwall", "v-champagne", "v-hurt-cash"]


def test_manual_choice_is_cached_for_next_transfers(store):
    worker, yt, tid = make(store)
    match_and_push(worker, tid)
    hurt = store.tracks(tid, ("not_found",))[0]
    worker.choose(hurt["id"], song("v-hurt-cash", "Hurt", ["Johnny Cash"], 218))

    searches_before = len(yt.searches)
    tid2 = store.create_transfer("p1", "Britpop", "", None, "PRIVATE")
    worker.run_once(tid2)
    assert len(yt.searches) == searches_before  # everything came from the cache
    assert store.count_tracks(tid2)["matched"] == 4


def test_second_export_only_adds_new_songs(store):
    worker, yt, tid = make(store)
    match_and_push(worker, tid)
    pid = store.get_transfer(tid)["yt_playlist_id"]

    worker.spotify.data["p1"] = TRACKS + [track("Champagne Supernova", ["Oasis"], 451, spotify_id="x2")]
    yt.catalogue.append(song("v-live", "Live Forever", ["Oasis"], 277))
    worker.spotify.data["p1"].append(track("Live Forever", ["Oasis"], 276))

    tid2 = store.create_transfer("p1", "Britpop", "", None, "PRIVATE")
    assert store.get_transfer(tid2)["yt_playlist_id"] == pid  # linked to the first export
    match_and_push(worker, tid2)
    assert yt.playlists == {pid: ["v-wonderwall", "v-champagne", "v-live"]}


def test_recreates_playlist_deleted_on_youtube(store):
    worker, yt, tid = make(store)
    match_and_push(worker, tid)
    yt.playlists.clear()

    tid2 = store.create_transfer("p1", "Britpop", "", None, "PRIVATE")
    match_and_push(worker, tid2)
    new_pid = store.get_transfer(tid2)["yt_playlist_id"]
    assert yt.playlists[new_pid] == ["v-wonderwall", "v-champagne"]


def test_error_is_reported_and_resumable(store):
    worker, yt, tid = make(store)
    original = yt.add_items

    def boom(*args):
        raise RuntimeError("YouTube said no")

    yt.add_items = boom
    match_and_push(worker, tid)
    transfer = store.get_transfer(tid)
    assert transfer["status"] == "error"
    assert "YouTube said no" in transfer["error"]

    yt.add_items = original
    worker.retry(tid)
    assert store.get_transfer(tid)["pending_action"] == "push"
    worker.run_once(tid)
    assert store.get_transfer(tid)["status"] == "done"


def test_cancel_stops_matching(store):
    worker, yt, tid = make(store)
    worker.cancel(tid)
    worker.run_once(tid)
    assert store.get_transfer(tid)["status"] == "cancelled"
    assert yt.searches == []


def test_skip_and_retry_unresolved(store):
    worker, yt, tid = make(store)
    worker.run_once(tid)
    hurt = store.tracks(tid, ("not_found",))[0]
    worker.skip(hurt["id"])
    assert store.count_tracks(tid)["skipped"] == 1

    worker.retry_unresolved(tid)  # skipped is a manual decision: kept
    worker.run_once(tid)
    assert store.count_tracks(tid)["skipped"] == 1


def test_isrc_is_searched_first_and_only_its_top_result_kept(store):
    from spot2ytm.jobs import find_match
    from spot2ytm.models import Track

    class ISRCYT(FakeYT):
        def search(self, query, filter="songs", limit=10):
            self.searches.append((query, filter))
            if query == "GBAAA9500001":
                return [song("v-exact", "Wonderwall", ["Oasis"], 258), song("v-junk", "Other", ["Someone"], 100)]
            return super().search(query, filter, limit)

    yt = ISRCYT(CATALOGUE)
    t = Track(title="Wonderwall", artists=["Oasis"], duration_ms=258000, spotify_id="w", isrc="GBAAA9500001")
    status, best, candidates = find_match(yt, t)
    assert status == "matched" and best.video_id == "v-exact"
    assert yt.searches == [("GBAAA9500001", "songs")]  # confident after one search
    assert [c.video_id for c in candidates] == ["v-exact"]


def test_key_for_name_ignores_case_and_underscores(store):
    tid = store.create_transfer("public:playlist:abc", "Dark_Metal", "", None, "PRIVATE")
    assert store.key_for_name("dark  metal") == "public:playlist:abc"
    assert store.key_for_name("Darker Metal") is None
    assert tid


def test_live_activity_while_matching(store):
    worker, yt, tid = make(store)
    seen = []
    search = yt.search

    def watching_search(query, filter="songs", limit=10):
        seen.append(worker.activity(tid))
        return search(query, filter, limit)

    yt.search = watching_search
    worker.run_once(tid)

    first = seen[0]
    assert first["phase"] == "matching" and first["total"] == 4
    assert first["song"]["title"] == "Wonderwall - Remastered"
    assert first["step"] == {"kind": "songs", "query": "Oasis Wonderwall"}
    # Songs already searched show up newest first, with what was found.
    titles = [r["title"] for r in seen[-1]["recent"]]
    assert titles.index("Champagne Supernova") < titles.index("Wonderwall - Remastered")
    wonderwall = seen[-1]["recent"][titles.index("Wonderwall - Remastered")]
    assert wonderwall["match"]["title"] == "Wonderwall" and wonderwall["via"] == "songs"
    assert wonderwall["status"] == "matched"
    # Gone once the worker moves on.
    assert worker.activity(tid) is None
