import json

import pytest
from fastapi.testclient import TestClient

from spot2ytm.web import create_app

from .conftest import FakeSpotify, FakeYT, song, track


@pytest.fixture
def app_and_client(settings):
    spotify = FakeSpotify({"p1": [track("Wonderwall", ["Oasis"], 258), track("Hurt", ["Johnny Cash"], 218)]})
    yt = FakeYT([song("v1", "Wonderwall", ["Oasis"], 259)])
    app = create_app(settings, spotify=spotify, yt=yt, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield app, client


def test_dashboard_and_playlists_render(app_and_client):
    _, client = app_and_client
    assert "Connected as" in client.get("/").text
    page = client.get("/playlists")
    assert page.status_code == 200
    assert "Playlist p1" in page.text and "Liked Songs" in page.text


def test_transfer_flow_and_review(app_and_client):
    app, client = app_and_client
    resp = client.post("/transfers", data={"playlist_id": "p1", "privacy": "PRIVATE"}, follow_redirects=False)
    assert resp.status_code == 303
    tid = int(resp.headers["location"].rsplit("/", 1)[1])
    app.state.worker.run_once(tid)

    page = client.get(f"/transfers/{tid}")
    assert "Needs review" in page.text and "Hurt" in page.text

    hurt = app.state.store.tracks(tid, ("not_found",))[0]
    cand = song("v2", "Hurt", ["Johnny Cash"], 218).to_dict()
    row = client.post(f"/tracks/{hurt['id']}/choose", data={"candidate": json.dumps(cand)})
    assert row.status_code == 200 and "your pick" in row.text
    assert row.headers["hx-trigger"] == "counts-changed"

    client.post(f"/transfers/{tid}/push")
    app.state.worker.run_once(tid)
    transfer = app.state.store.get_transfer(tid)
    assert transfer["status"] == "done"
    assert app.state.yt.playlists[transfer["yt_playlist_id"]] == ["v1", "v2"]

    csv = client.get(f"/transfers/{tid}/export.csv")
    assert csv.status_code == 200 and "Wonderwall" in csv.text


def test_search_partial(app_and_client):
    app, client = app_and_client
    resp = client.post("/transfers", data={"playlist_id": "p1"}, follow_redirects=False)
    tid = int(resp.headers["location"].rsplit("/", 1)[1])
    app.state.worker.run_once(tid)
    first = app.state.store.tracks(tid)[0]
    html = client.get(f"/tracks/{first['id']}/search", params={"q": "wonderwall"}).text
    assert "Use this" in html or "current" in html


def test_cross_site_post_refused(app_and_client):
    _, client = app_and_client
    resp = client.post("/auth/ytmusic/disconnect", headers={"origin": "https://evil.example"})
    assert resp.status_code == 403


def test_foreign_host_refused(app_and_client):
    _, client = app_and_client
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_spotify_login_redirect(app_and_client):
    _, client = app_and_client
    resp = client.get("/auth/spotify/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("https://accounts.spotify.com/authorize")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fauth%2Fspotify%2Fcallback" in resp.headers["location"]


def test_callback_rejects_unknown_state(app_and_client):
    _, client = app_and_client
    resp = client.get("/auth/spotify/callback", params={"state": "nope", "code": "x"}, follow_redirects=False)
    assert "error" in resp.headers["location"]
