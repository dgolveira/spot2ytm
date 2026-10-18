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


def test_live_activity_partial(app_and_client):
    from spot2ytm.jobs import Activity

    app, client = app_and_client
    tid = int(client.post("/transfers", data={"playlist_id": "p1"}, follow_redirects=False)
              .headers["location"].rsplit("/", 1)[1])
    activity = Activity("matching", 0, 2)
    activity.finish_song({"position": 1, "title": "Wonderwall", "artists": ["Oasis"], "status": "matched",
                          "score": 0.97, "via": "isrc",
                          "match": {"title": "Wonderwall", "artists": ["Oasis"], "type": "song"}})
    activity.update(song={"position": 2, "title": "Hurt", "artists": ["Johnny Cash"]},
                    step={"kind": "isrc", "query": "USUM70210590"})
    app.state.worker._activity[tid] = activity
    app.state.store.update_transfer(tid, status="matching")

    page = client.get(f"/transfers/{tid}/activity")
    assert page.status_code == 200
    assert "Searching YouTube Music" in page.text and "USUM70210590" in page.text
    assert "97%" in page.text and "by ISRC" in page.text
    assert 'class="eq"' in client.get(f"/transfers/{tid}").text

    app.state.store.update_transfer(tid, status="review")
    assert client.get(f"/transfers/{tid}/activity").status_code == 286


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


def test_spotify_premium_error_offers_disconnect(app_and_client, settings):
    from spot2ytm.spotify import SpotifyPremiumError

    app, client = app_and_client
    settings.save_spotify_client_id("0123456789abcdef0123456789abcdef")

    def refuse():
        raise SpotifyPremiumError("premium required")

    app.state.spotify.me = refuse
    app.state.spotify.liked_songs_playlist = refuse
    page = client.get("/").text
    assert "needs an active Premium subscription" in page and 'action="/auth/spotify/forget"' in page
    playlists = client.get("/playlists")
    assert playlists.status_code == 502 and "Import from Exportify" in playlists.text

    resp = client.post("/auth/spotify/forget", follow_redirects=False)
    assert resp.status_code == 303
    assert settings.spotify_client_id == ""
    assert not app.state.spotify.connected
    assert "Set up Spotify" in client.get("/").text


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


def test_connect_from_browser_errors(app_and_client, settings):
    from spot2ytm.ytm import YTMError, YTMusicClient

    yt = YTMusicClient(settings.ytmusic_auth_file)
    with pytest.raises(YTMError, match="Unknown browser"):
        yt.connect_from_browser("netscape")
    with pytest.raises(YTMError, match="No YouTube login found in Microsoft Edge"):
        yt.connect_from_browser("edge", read_cookies=lambda b: {"PREF": "x"})

    def locked(browser):
        raise PermissionError("database is locked")

    with pytest.raises(YTMError, match="Could not read the cookies of Firefox: database is locked"):
        yt.connect_from_browser("firefox", read_cookies=locked)
    assert not yt.connected


def test_ytmusic_setup_page_offers_browsers(app_and_client):
    _, client = app_and_client
    page = client.get("/setup/ytmusic").text
    assert "Connect from browser" in page and "Microsoft Edge" in page
