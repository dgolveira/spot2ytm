"""Scoring YouTube Music search results against a Spotify track.

A score combines title, artist and duration similarity, with penalties when
the candidate is a different version of the song (live, remix, sped up...).
It lands roughly in [0, 1]:

* ``>= MATCH_THRESHOLD``  - confident, added automatically
* ``>= REVIEW_THRESHOLD`` - probably right, but shown for review
* below that              - treated as not found
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from .models import Candidate, Track

MATCH_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.55

# Words that mark a *different recording* of the song. A candidate carrying
# one that the Spotify title does not have is most likely the wrong version.
VERSION_TAGS = {
    "live": r"\blive\b|\bao vivo\b|\ben vivo\b",
    "remix": r"\bremix(es)?\b|\brmx\b|\bmix\b(?! ?tape)",
    "acoustic": r"\bacoustic\b|\bacustico\b|\bunplugged\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b|\bbacking track\b",
    "cover": r"\bcover\b|\btribute\b|\bin the style of\b",
    "sped_up": r"\bsped ?up\b|\bspeed ?up\b|\bnightcore\b",
    "slowed": r"\bslowed\b|\breverb\b",
    "demo": r"\bdemo\b",
    "8d": r"\b8d\b",
    "acapella": r"\ba ?cappella\b|\bacapella\b",
    "orchestral": r"\borchestral\b|\bsymphonic\b",
    "lofi": r"\blo-?fi\b",
}
_VERSION_RES = {tag: re.compile(rx) for tag, rx in VERSION_TAGS.items()}

_FEAT_RE = re.compile(r"\s*[\(\[]?\b(feat\.?|ft\.?|featuring)\s.*?([\)\]]|$)", re.IGNORECASE)
_BRACKETS_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_ARTIST_NOISE_RE = re.compile(r"(\s+topic|vevo|\s+official)$")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize(text: str) -> str:
    text = strip_accents(text).lower()
    text = text.replace("&", " and ").replace("’", "'")
    text = re.sub(r"[^\w\s']", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def version_tags(title: str) -> set[str]:
    text = normalize(title)
    return {tag for tag, rx in _VERSION_RES.items() if rx.search(text)}


def base_title(title: str, artists: list[str] | None = None) -> str:
    """The song name without featured artists, remaster notes, video tags..."""
    text = _FEAT_RE.sub(" ", title)
    # Drop bracketed segments entirely: they are either noise or version tags,
    # and version tags are scored separately.
    text = _BRACKETS_RE.sub(" ", text)
    # "Song - 2011 Remaster", "Song - Live at Wembley"
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", text) if p.strip()]
    if artists and len(parts) > 1:
        # YouTube videos are often titled "Artist - Song".
        first = normalize(parts[0])
        if any(fuzz.ratio(first, normalize(a)) >= 85 for a in artists):
            parts = parts[1:]
    text = parts[0] if parts else text
    return normalize(text) or normalize(title)


def clean_artist(name: str) -> str:
    return _ARTIST_NOISE_RE.sub("", normalize(name)).strip()


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b), 0.9 * fuzz.token_set_ratio(a, b)) / 100


def artist_similarity(source: list[str], candidate: list[str]) -> float:
    src = [clean_artist(a) for a in source if a]
    cand = [clean_artist(a) for a in candidate if a]
    if not src or not cand:
        return 0.0
    # Best match for the primary artist (YouTube often lists only the first one).
    primary = max(fuzz.ratio(src[0], c) for c in cand)
    # YouTube sometimes merges everyone into a single "A, B & C" artist.
    joined = fuzz.token_set_ratio(" ".join(src), " ".join(cand))
    return max(primary, joined) / 100


def duration_similarity(duration_ms: int, duration_s: int | None) -> float:
    if not duration_ms or not duration_s:
        return 0.5  # unknown: neither reward nor punish
    diff = abs(duration_ms / 1000 - duration_s)
    if diff <= 3:
        return 1.0
    return max(0.0, 1 - (diff - 3) / 27)


def score(track: Track, cand: Candidate) -> float:
    src_title = base_title(track.title)
    cand_title = base_title(cand.title, track.artists)
    t = title_similarity(src_title, cand_title)

    a = artist_similarity(track.artists, cand.artists)
    if cand.result_type == "video" and a < 0.6:
        # Videos from random channels: the artist may only appear in the title.
        title_norm = normalize(cand.title)
        if track.artists and normalize(track.artists[0]) in title_norm:
            a = max(a, 0.9)

    d = duration_similarity(track.duration_ms, cand.duration_s)

    total = 0.5 * t + 0.3 * a + 0.2 * d
    if a < 0.5:
        # Same title by someone else is usually another song (or a cover),
        # unless the length agrees, e.g. artist names written in another script.
        total -= 0.25 * (1 - d)

    src_tags = version_tags(track.title) | version_tags(track.album)
    cand_tags = version_tags(cand.title)
    extra = cand_tags - src_tags
    missing = version_tags(track.title) - cand_tags
    total -= min(0.5, 0.3 * len(extra))
    total -= min(0.3, 0.15 * len(missing))

    if cand.album and track.album and fuzz.ratio(base_title(cand.album), base_title(track.album)) >= 90:
        total += 0.03
    if cand.result_type == "video":
        total -= 0.05  # prefer the official audio track when one exists
    return max(0.0, min(1.0, total))


def rank(track: Track, candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    unique = []
    for cand in candidates:
        if cand.video_id and cand.video_id not in seen:
            seen.add(cand.video_id)
            cand.score = score(track, cand)
            unique.append(cand)
    unique.sort(key=lambda c: c.score, reverse=True)
    return unique


def status_for(best_score: float | None) -> str:
    if best_score is None:
        return "not_found"
    if best_score >= MATCH_THRESHOLD:
        return "matched"
    if best_score >= REVIEW_THRESHOLD:
        return "uncertain"
    return "not_found"


def search_queries(track: Track) -> list[tuple[str, str]]:
    """(query, filter) pairs to try in order, most specific first."""
    title = _FEAT_RE.sub(" ", track.title)
    title = re.sub(r"\s+-\s+.*remaster.*$", "", title, flags=re.IGNORECASE).strip()
    primary = track.artists[0] if track.artists else ""
    everyone = " ".join(track.artists[:3])
    queries = [(f"{primary} {title}".strip(), "songs")]
    if len(track.artists) > 1:
        queries.append((f"{title} {everyone}".strip(), "songs"))
    queries.append((f"{primary} {title}".strip(), "videos"))
    return queries
