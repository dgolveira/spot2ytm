# Contributing to spot2ytm

Thanks for wanting to help! Bug reports, matching improvements, translations and
docs fixes are all welcome.

## Ground rules

- Be kind. This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Security problems go through [private reporting](SECURITY.md), not public issues.

## Development setup

```bash
git clone https://github.com/dgolveira/spot2ytm.git
cd spot2ytm
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
spot2ytm                           # http://127.0.0.1:8000
```

The test suite uses in-memory fakes for Spotify and YouTube Music, so it needs no
accounts and no network.

## Project layout

```
spot2ytm/
├── spotify.py     # Spotify Web API client (OAuth PKCE, playlists, liked songs)
├── ytm.py         # YouTube Music wrapper around ytmusicapi
├── matching.py    # Scoring of search results against a Spotify track
├── jobs.py        # Background worker: match, review, push
├── db.py          # SQLite storage
├── web.py         # FastAPI routes
├── templates/     # Jinja2 + htmx pages and partials
└── static/        # CSS, JS, htmx
tests/             # pytest suite
```

## Improving the matcher

The matcher is the heart of the project. If a song is matched wrongly (or not at all):

1. Add a failing case to `tests/test_matching.py` with the Spotify title/artists/duration
   and the YouTube Music result(s).
2. Adjust `spot2ytm/matching.py` until the new test passes **and** the old ones still do.
3. Mention in the PR which real songs you checked.

## Pull requests

- Keep PRs focused: one change per PR.
- Add or update tests for behaviour changes.
- Run `pytest` before pushing; CI runs it on Python 3.11 to 3.13.
- Update `CHANGELOG.md` under **Unreleased**.
- Use clear commit messages (`fix: ...`, `feat: ...`, `docs: ...` are appreciated).
