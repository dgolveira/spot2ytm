import pytest

from spot2ytm import matching
from spot2ytm.matching import base_title, rank, score, status_for, version_tags

from .conftest import song, track


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Wonderwall - Remastered", "wonderwall"),
        ("Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody"),
        ("Get Lucky (feat. Pharrell Williams and Nile Rodgers)", "get lucky"),
        ("Águas de Março", "aguas de marco"),
        ("Don't Stop Me Now", "dont stop me now"),
        ("Rock & Roll", "rock and roll"),
    ],
)
def test_base_title(title, expected):
    assert base_title(title) == expected


def test_base_title_strips_artist_prefix_of_videos():
    assert base_title("Oasis - Wonderwall (Official Video)", ["Oasis"]) == "wonderwall"


@pytest.mark.parametrize(
    "title, tags",
    [
        ("Hotel California - Live On MTV, 1994", {"live"}),
        ("Ai Se Eu Te Pego - Ao Vivo", {"live"}),
        ("Despacito - Remix", {"remix"}),
        ("Blinding Lights (Sped Up)", {"sped_up"}),
        ("Wonderwall - Remastered", set()),
        ("Mixtape Song", set()),
    ],
)
def test_version_tags(title, tags):
    assert version_tags(title) == tags


def test_exact_match_scores_high():
    t = track("Wonderwall - Remastered", ["Oasis"], 258)
    c = song("a", "Wonderwall", ["Oasis"], 259, album="(What's The Story) Morning Glory?")
    assert score(t, c) >= matching.MATCH_THRESHOLD


def test_accents_and_artist_spelling():
    t = track("Tempo Perdido", ["Legião Urbana"], 302)
    c = song("a", "Tempo Perdido", ["Legiao Urbana"], 303)
    assert status_for(score(t, c)) == "matched"


def test_live_version_is_penalised_when_studio_wanted():
    t = track("Hotel California", ["Eagles"], 391)
    studio = song("studio", "Hotel California", ["Eagles"], 391)
    live = song("live", "Hotel California (Live On MTV, 1994)", ["Eagles"], 433)
    ranked = rank(t, [live, studio])
    assert ranked[0].video_id == "studio"
    assert status_for(ranked[1].score) != "matched"


def test_live_version_wins_when_live_wanted():
    t = track("Hotel California - Live On MTV, 1994", ["Eagles"], 431)
    studio = song("studio", "Hotel California", ["Eagles"], 391)
    live = song("live", "Hotel California (Live On MTV, 1994)", ["Eagles"], 433)
    assert rank(t, [studio, live])[0].video_id == "live"


def test_sped_up_and_karaoke_rejected():
    t = track("Blinding Lights", ["The Weeknd"], 200)
    for title in ("Blinding Lights (Sped Up)", "Blinding Lights (Karaoke Version)"):
        assert status_for(score(t, song("x", title, ["The Weeknd"], 170))) != "matched"


def test_wrong_artist_is_not_matched():
    t = track("Hurt", ["Johnny Cash"], 218)
    c = song("nin", "Hurt", ["Nine Inch Nails"], 373)
    assert status_for(score(t, c)) == "not_found"


def test_big_duration_difference_goes_to_review():
    t = track("Garota de Ipanema", ["Tom Jobim"], 323)
    c = song("a", "Garota de Ipanema", ["Tom Jobim"], 243)
    assert status_for(score(t, c)) == "uncertain"


def test_video_with_artist_in_title_is_accepted_but_songs_preferred():
    t = track("Wonderwall", ["Oasis"], 258)
    video = song("v", "Oasis - Wonderwall (Official Video)", ["OasisVEVO"], 259, result_type="video")
    audio = song("s", "Wonderwall", ["Oasis"], 259)
    assert score(t, video) >= matching.REVIEW_THRESHOLD
    assert rank(t, [video, audio])[0].video_id == "s"


def test_rank_dedupes():
    t = track("Song", ["Artist"], 200)
    c = song("same", "Song", ["Artist"], 200)
    assert len(rank(t, [c, song("same", "Song", ["Artist"], 200)])) == 1


def test_status_for():
    assert status_for(None) == "not_found"
    assert status_for(0.99) == "matched"
    assert status_for(0.6) == "uncertain"
    assert status_for(0.1) == "not_found"


def test_search_queries():
    t = track("Get Lucky (feat. Pharrell Williams)", ["Daft Punk", "Pharrell Williams"], 369)
    queries = matching.search_queries(t)
    assert queries[0] == ("Daft Punk Get Lucky", "songs")
    assert ("Daft Punk Get Lucky", "videos") in queries
