import json

import httpx
import pytest
from fastapi.testclient import TestClient

from spot2ytm.public import PublicSpotify, PublicSpotifyError, parse_link, split_artists, track_ids
from spot2ytm.web import create_app

from .conftest import FakeSpotify, FakeYT, song

PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
WONDERWALL = "1qPbGZqppFwLwcBC1JQ6Vr"
HURT = "28cnXtME493VX9NOw9cIUh"


def page(entity: dict | None) -> str:
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


ENTITIES = {
    f"/embed/playlist/{PLAYLIST_ID}": {
        "type": "playlist", "name": "Britpop", "subtitle": "Me",
        "coverArt": {"sources": [{"url": "https://i.scdn.co/image/x"}]},
        "trackList": [
            {"uri": f"spotify:track:{WONDERWALL}", "title": "Wonderwall", "subtitle": "Oasis",
             "duration": 258000, "entityType": "track"},
            {"uri": "spotify:episode:abc", "title": "A podcast", "subtitle": "Show",
             "duration": 1000, "entityType": "episode"},
        ],
    },
    f"/embed/track/{WONDERWALL}": {
        "type": "track", "title": "Wonderwall", "artists": [{"name": "Oasis"}], "duration": 258000,
    },
}


def transport():
    def handler(request: httpx.Request) -> httpx.Response:
        entity = ENTITIES.get(request.url.path)
        return httpx.Response(200, text=page(entity))
    return httpx.MockTransport(handler)


def test_parse_link_and_track_ids():
    assert parse_link(f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=abc") == ("playlist", PLAYLIST_ID)
    assert parse_link(f"https://open.spotify.com/intl-pt/album/{PLAYLIST_ID}") == ("album", PLAYLIST_ID)
    assert parse_link(f"spotify:playlist:{PLAYLIST_ID}") == ("playlist", PLAYLIST_ID)
    assert parse_link("https://open.spotify.com/user/someone") is None
    pasted = f"https://open.spotify.com/track/{WONDERWALL}\r\nhttps://open.spotify.com/track/{HURT}\n" \
             f"https://open.spotify.com/track/{WONDERWALL}\n"
    assert track_ids(pasted) == [WONDERWALL, HURT, WONDERWALL]


def test_split_artists_keeps_commas_inside_names():
    assert split_artists("Tyler, The Creator, Kali Uchis") == ["Tyler, The Creator", "Kali Uchis"]


def test_collection_reads_songs_and_skips_episodes():
    playlist, tracks = PublicSpotify(transport(), delay=0).collection("playlist", PLAYLIST_ID)
    assert playlist.id == f"public:playlist:{PLAYLIST_ID}"
    assert playlist.name == "Britpop" and playlist.image_url == "https://i.scdn.co/image/x"
    assert [(t.title, t.artists, t.duration_ms, t.spotify_id) for t in tracks] == [
        ("Wonderwall", ["Oasis"], 258000, WONDERWALL)
    ]


def test_missing_page_is_an_error():
    with pytest.raises(PublicSpotifyError, match="not found"):
        PublicSpotify(transport(), delay=0).track(HURT)


@pytest.fixture
def app_and_client(settings):
    yt = FakeYT([song("v1", "Wonderwall", ["Oasis"], 259)])
    app = create_app(settings, spotify=FakeSpotify(), yt=yt, start_worker=False,
                     public=PublicSpotify(transport(), delay=0))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield app, client


def test_import_playlist_link(app_and_client):
    app, client = app_and_client
    resp = client.post("/import", data={"source": f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
                                        "auto_push": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    tid = int(resp.headers["location"].rsplit("/", 1)[1])
    app.state.worker.run_once(tid)
    transfer = app.state.store.get_transfer(tid)
    assert transfer["status"] == "done" and transfer["name"] == "Britpop"
    assert app.state.yt.playlists[transfer["yt_playlist_id"]] == ["v1"]


def test_import_pasted_song_links(app_and_client):
    app, client = app_and_client
    pasted = f"https://open.spotify.com/track/{WONDERWALL}\nhttps://open.spotify.com/track/{HURT}"
    resp = client.post("/import", data={"source": pasted, "name": "Liked Songs"}, follow_redirects=False)
    tid = int(resp.headers["location"].rsplit("/", 1)[1])
    app.state.worker.run_once(tid)

    rows = app.state.store.tracks(tid)
    assert [(r["title"], r["status"]) for r in rows] == [
        ("Wonderwall", "matched"), (f"Unavailable song ({HURT})", "not_found"),
    ]
    assert app.state.store.get_transfer(tid)["status"] == "review"

    # Importing again under the same name targets the same YouTube Music playlist.
    client.post(f"/transfers/{tid}/push")
    app.state.worker.run_once(tid)
    again = client.post("/import", data={"source": pasted, "name": "liked songs"}, follow_redirects=False)
    tid2 = int(again.headers["location"].rsplit("/", 1)[1])
    store = app.state.store
    assert store.get_transfer(tid2)["yt_playlist_id"] == store.get_transfer(tid)["yt_playlist_id"]


def test_import_rejects_other_text(app_and_client):
    _, client = app_and_client
    resp = client.post("/import", data={"source": "https://open.spotify.com/user/someone"})
    assert resp.status_code == 400 and "Paste a Spotify playlist" in resp.text
