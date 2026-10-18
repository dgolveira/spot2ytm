import json
import time

import httpx

import pytest

from spot2ytm.spotify import (
    SpotifyClient, SpotifyPremiumError, authorize_url, make_pkce_pair, parse_playlist, parse_track_entry,
)


def item(name, artists, track_id="t1", **extra):
    return {
        "type": "track", "id": track_id, "name": name, "duration_ms": 200000,
        "artists": [{"name": a} for a in artists], "album": {"name": "Album"},
        "external_ids": {"isrc": "GBXXX"}, **extra,
    }


def test_parse_entry_new_and_old_field_names():
    new = parse_track_entry({"item": item("Song", ["A", "B"])})
    old = parse_track_entry({"track": item("Song", ["A", "B"])})
    assert new == old
    assert new.artists == ["A", "B"]
    assert new.isrc == "GBXXX"


def test_parse_entry_skips_episodes_and_gaps():
    assert parse_track_entry({"item": None}) is None
    assert parse_track_entry({"item": {"type": "episode", "name": "Podcast"}}) is None


def test_local_files_have_no_spotify_id():
    t = parse_track_entry({"is_local": True, "item": item("Home Demo", ["Me"], track_id=None)})
    assert t.spotify_id is None
    assert t.cache_key.startswith("local:")


def test_parse_playlist_readability():
    mine = parse_playlist({"id": "p", "name": "Mine", "owner": {"id": "me"}, "items": {"total": 3}}, "me")
    theirs = parse_playlist({"id": "q", "name": "Theirs", "owner": {"id": "x"}, "tracks": {"total": 9}}, "me")
    collab = parse_playlist({"id": "r", "name": "Collab", "owner": {"id": "x"}, "collaborative": True}, "me")
    assert (mine.readable, mine.total) == (True, 3)
    assert (theirs.readable, theirs.total) == (False, 9)
    assert collab.readable


def test_description_is_unescaped():
    p = parse_playlist({"id": "p", "name": "x", "owner": {"id": "me"},
                        "description": "Rock &amp; roll <a href=\"spotify:x\">link</a>"}, "me")
    assert p.description == "Rock & roll link"


def test_pkce_and_authorize_url():
    verifier, challenge = make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    url = authorize_url("cid", "http://127.0.0.1:8000/auth/spotify/callback", "st", challenge)
    assert "code_challenge_method=S256" in url and "client_id=cid" in url


def test_paginates_items_and_refreshes_token(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"access_token": "old", "refresh_token": "r", "expires_at": 0}))
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new"
        if "offset=50" in str(request.url):
            return httpx.Response(200, json={"items": [{"item": item("Two", ["B"], "t2")}], "next": None})
        return httpx.Response(200, json={
            "items": [{"item": item("One", ["A"], "t1")}],
            "next": "https://api.spotify.com/v1/playlists/p/items?offset=50&limit=50",
        })

    client = SpotifyClient("cid", token_file, transport=httpx.MockTransport(handler))
    tracks = client.playlist_tracks("p")
    assert [t.title for t in tracks] == ["One", "Two"]
    assert "/playlists/p/items" in calls[1]
    stored = json.loads(token_file.read_text())
    assert stored["access_token"] == "new" and stored["refresh_token"] == "r"
    assert stored["expires_at"] > time.time()


def test_premium_required_is_its_own_error(tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 3600}))

    def handler(request: httpx.Request):
        return httpx.Response(403, text="Active premium subscription required for the owner of the app.")

    client = SpotifyClient("cid", token_file, transport=httpx.MockTransport(handler))
    with pytest.raises(SpotifyPremiumError):
        client.me()
