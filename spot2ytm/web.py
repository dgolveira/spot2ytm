"""FastAPI application: pages, HTMX partials and OAuth callbacks."""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, matching
from .config import Settings
from .db import ACTIVE_STATUSES, Store, row_candidates, row_track
from .jobs import Worker, new_transfer
from .models import Candidate, Track
from .public import EMBED_TRACK_LIMIT, PublicSpotify, PublicSpotifyError, parse_link, track_ids
from .spotify import LIKED_SONGS_ID, SpotifyAuthError, SpotifyClient, SpotifyError, authorize_url, make_pkce_pair
from .ytm import YTMError, YTMusicClient

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PRIVACY_OPTIONS = ("PRIVATE", "UNLISTED", "PUBLIC")
TRACK_FILTERS = {
    "review": ("uncertain", "not_found"),
    "matched": ("matched",),
    "uncertain": ("uncertain",),
    "not_found": ("not_found",),
    "skipped": ("skipped",),
    "all": None,
}


def fmt_duration(seconds: float | int | None) -> str:
    if not seconds:
        return ""
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def create_app(
    settings: Settings | None = None,
    spotify: SpotifyClient | None = None,
    yt: YTMusicClient | None = None,
    start_worker: bool = True,
    public: PublicSpotify | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.db_path)
    yt = yt or YTMusicClient(settings.ytmusic_auth_file)
    spotify = spotify or SpotifyClient(settings.spotify_client_id, settings.spotify_token_file)
    public = public or PublicSpotify()
    worker = Worker(store, spotify, yt, settings.search_delay, public=public)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield

    app = FastAPI(title="spot2ytm", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.spotify = spotify
    app.state.yt = yt
    app.state.worker = worker
    app.state.oauth_pending = {}  # state -> PKCE verifier

    # The app holds your credentials and listens on localhost: refuse requests
    # whose Host is not ours (DNS rebinding) and cross-site form posts (CSRF).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response("Cross-site request refused", status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["duration"] = fmt_duration
    templates.env.filters["fromjson"] = json.loads
    templates.env.globals["version"] = __version__
    templates.env.globals["MATCH_THRESHOLD"] = matching.MATCH_THRESHOLD
    templates.env.globals["EMBED_TRACK_LIMIT"] = EMBED_TRACK_LIMIT

    def render(request: Request, name: str, status_code: int = 200, headers: dict | None = None, **ctx):
        ctx.setdefault("spotify_connected", app.state.spotify.connected)
        ctx.setdefault("yt_connected", app.state.yt.connected)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code, headers=headers)

    def transfer_or_404(transfer_id: int):
        transfer = store.get_transfer(transfer_id)
        if transfer is None:
            raise HTTPException(404, "Transfer not found")
        return transfer

    def track_or_404(track_id: int):
        row = store.get_track(track_id)
        if row is None:
            raise HTTPException(404, "Track not found")
        return row

    def transfer_summaries():
        rows = store.list_transfers()
        return [(t, store.count_tracks(t["id"])) for t in rows]

    # -- dashboard ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        spotify_user = None
        spotify_error = None
        if app.state.spotify.connected:
            try:
                spotify_user = app.state.spotify.me()
            except SpotifyError as exc:
                spotify_error = str(exc)
        return render(
            request, "index.html",
            spotify_user=spotify_user,
            spotify_error=spotify_error,
            yt_account=app.state.yt.account(),
            client_id_set=bool(settings.spotify_client_id),
            transfers=transfer_summaries(),
            active_statuses=ACTIVE_STATUSES,
        )

    @app.get("/partials/transfers", response_class=HTMLResponse)
    def transfers_partial(request: Request):
        return render(request, "partials/transfer_list.html", transfers=transfer_summaries(),
                      active_statuses=ACTIVE_STATUSES)

    # -- Spotify --------------------------------------------------------------

    @app.get("/setup/spotify", response_class=HTMLResponse)
    def setup_spotify(request: Request, error: str | None = None):
        return render(request, "setup_spotify.html", client_id=settings.spotify_client_id,
                      redirect_uri=settings.spotify_redirect_uri, error=error)

    @app.post("/setup/spotify")
    def save_spotify(client_id: str = Form(...)):
        client_id = client_id.strip()
        if not client_id.isalnum() or len(client_id) < 16:
            return RedirectResponse("/setup/spotify?error=That+does+not+look+like+a+Spotify+client+ID", 303)
        if client_id != settings.spotify_client_id:
            settings.save_spotify_client_id(client_id)
            app.state.spotify.disconnect()
            app.state.spotify = SpotifyClient(client_id, settings.spotify_token_file)
            worker.spotify = app.state.spotify
        return RedirectResponse("/auth/spotify/login", 303)

    @app.get("/auth/spotify/login")
    def spotify_login():
        if not settings.spotify_client_id:
            return RedirectResponse("/setup/spotify", 303)
        state = secrets.token_urlsafe(24)
        verifier, challenge = make_pkce_pair()
        app.state.oauth_pending = {state: verifier}  # only the latest attempt is valid
        return RedirectResponse(
            authorize_url(settings.spotify_client_id, settings.spotify_redirect_uri, state, challenge), 303
        )

    @app.get("/auth/spotify/callback")
    def spotify_callback(state: str = "", code: str = "", error: str = ""):
        if error:
            return RedirectResponse(f"/setup/spotify?error=Spotify+said:+{error}", 303)
        verifier = app.state.oauth_pending.pop(state, None)
        if not verifier or not code:
            return RedirectResponse("/setup/spotify?error=Login+expired,+please+try+again", 303)
        try:
            app.state.spotify.exchange_code(code, settings.spotify_redirect_uri, verifier)
        except SpotifyError as exc:
            log.warning("spotify token exchange failed: %s", exc)
            return RedirectResponse("/setup/spotify?error=Could+not+finish+the+Spotify+login", 303)
        return RedirectResponse("/", 303)

    @app.post("/auth/spotify/disconnect")
    def spotify_disconnect():
        app.state.spotify.disconnect()
        return RedirectResponse("/", 303)

    # -- YouTube Music --------------------------------------------------------

    @app.get("/setup/ytmusic", response_class=HTMLResponse)
    def setup_ytmusic(request: Request):
        return render(request, "setup_ytmusic.html", error=None, headers_raw="")

    @app.post("/setup/ytmusic", response_class=HTMLResponse)
    def save_ytmusic(request: Request, headers_raw: str = Form("")):
        try:
            app.state.yt.connect_with_headers(headers_raw)
        except YTMError as exc:
            return render(request, "setup_ytmusic.html", status_code=400, error=str(exc), headers_raw=headers_raw)
        return RedirectResponse("/", 303)

    @app.post("/auth/ytmusic/disconnect")
    def ytmusic_disconnect():
        app.state.yt.disconnect()
        return RedirectResponse("/", 303)

    # -- playlists ------------------------------------------------------------

    @app.get("/playlists", response_class=HTMLResponse)
    def playlists(request: Request):
        if not app.state.spotify.connected:
            return RedirectResponse("/setup/spotify", 303)
        try:
            items = [app.state.spotify.liked_songs_playlist(), *app.state.spotify.playlists()]
        except SpotifyAuthError:
            return RedirectResponse("/setup/spotify?error=Please+connect+Spotify+again", 303)
        except SpotifyError as exc:
            return render(request, "error.html", status_code=502, message=f"Spotify error: {exc}")
        return render(
            request, "playlists.html",
            playlists=items,
            last=store.last_transfer_by_playlist(),
            privacy_options=PRIVACY_OPTIONS,
        )

    @app.post("/transfers")
    async def create_transfers(request: Request):
        form = await request.form()
        ids = [str(v) for v in form.getlist("playlist_id")]
        privacy = str(form.get("privacy") or "PRIVATE")
        if privacy not in PRIVACY_OPTIONS:
            privacy = "PRIVATE"
        auto_push = form.get("auto_push") == "1"
        if not ids:
            return RedirectResponse("/playlists", 303)
        if not app.state.yt.connected:
            return RedirectResponse("/setup/ytmusic", 303)
        created = []
        for playlist_id in ids:
            try:
                playlist = app.state.spotify.playlist(playlist_id)
            except SpotifyError as exc:
                log.warning("skipping playlist %s: %s", playlist_id, exc)
                continue
            transfer_id = new_transfer(store, playlist, privacy, auto_push)
            worker.enqueue(transfer_id)
            created.append(transfer_id)
        if len(created) == 1:
            return RedirectResponse(f"/transfers/{created[0]}", 303)
        return RedirectResponse("/", 303)

    # -- import without a Spotify login ----------------------------------------

    def import_form(request: Request, status_code: int = 200, error: str | None = None, **values):
        return render(request, "import.html", status_code=status_code, error=error,
                      privacy_options=PRIVACY_OPTIONS, values=values)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return import_form(request, auto_push=True)

    @app.post("/import", response_class=HTMLResponse)
    def import_submit(
        request: Request, source: str = Form(""), name: str = Form(""),
        privacy: str = Form("PRIVATE"), auto_push: str = Form(""),
    ):
        values = {"source": source, "name": name, "privacy": privacy, "auto_push": auto_push == "1"}
        if privacy not in PRIVACY_OPTIONS:
            privacy = "PRIVATE"
        if not app.state.yt.connected:
            return import_form(request, 400, "Connect YouTube Music first.", **values)
        ids = track_ids(source)
        link = parse_link(source)
        name = name.strip()[:150]
        if ids:
            title = name or "Imported from Spotify"
            # Same name, same YouTube Music playlist: importing again only adds new songs.
            key = store.key_for_name(title) or "paste:" + " ".join(title.lower().split())
            transfer_id = store.create_transfer(key, title, "", None, privacy, values["auto_push"])
            store.add_tracks(transfer_id, [Track(title="", artists=[], spotify_id=i) for i in ids])
        elif link and link[0] in ("playlist", "album"):
            try:
                playlist, tracks = public.collection(*link)
            except PublicSpotifyError as exc:
                return import_form(request, 400, str(exc), **values)
            if not tracks:
                return import_form(request, 400, "That playlist has no songs that can be read.", **values)
            if name:
                playlist.name = name
            transfer_id = new_transfer(store, playlist, privacy, values["auto_push"])
            store.add_tracks(transfer_id, tracks)
        else:
            return import_form(request, 400, "Paste a Spotify playlist or album link, or a list of song links.",
                               **values)
        worker.enqueue(transfer_id)
        return RedirectResponse(f"/transfers/{transfer_id}", 303)

    # -- transfers ------------------------------------------------------------

    @app.get("/transfers/{transfer_id}", response_class=HTMLResponse)
    def transfer_page(request: Request, transfer_id: int, filter: str | None = None):
        transfer = transfer_or_404(transfer_id)
        counts = store.count_tracks(transfer_id)
        if filter not in TRACK_FILTERS:
            filter = "review" if counts["uncertain"] + counts["not_found"] else "all"
        return render(
            request, "transfer.html",
            transfer=transfer,
            counts=counts,
            filter=filter,
            tracks=store.tracks(transfer_id, TRACK_FILTERS[filter]),
            active=transfer["status"] in ACTIVE_STATUSES,
            yt_url=YTMusicClient.playlist_url(transfer["yt_playlist_id"]) if transfer["yt_playlist_id"] else None,
        )

    @app.get("/transfers/{transfer_id}/status", response_class=HTMLResponse)
    def transfer_status(request: Request, transfer_id: int, was: str = ""):
        transfer = transfer_or_404(transfer_id)
        active = transfer["status"] in ACTIVE_STATUSES
        headers = {}
        if was in ACTIVE_STATUSES and not active:
            headers["HX-Refresh"] = "true"  # job finished: reload to show the track list
        return render(
            request, "partials/transfer_status.html", headers=headers,
            transfer=transfer, counts=store.count_tracks(transfer_id), active=active,
            yt_url=YTMusicClient.playlist_url(transfer["yt_playlist_id"]) if transfer["yt_playlist_id"] else None,
        )

    def back_to(transfer_id: int):
        return RedirectResponse(f"/transfers/{transfer_id}", 303)

    @app.post("/transfers/{transfer_id}/push")
    def push(transfer_id: int):
        transfer_or_404(transfer_id)
        if not app.state.yt.connected:
            return RedirectResponse("/setup/ytmusic", 303)
        worker.request(transfer_id, "push")
        return back_to(transfer_id)

    @app.post("/transfers/{transfer_id}/retry")
    def retry(transfer_id: int):
        transfer_or_404(transfer_id)
        worker.retry(transfer_id)
        return back_to(transfer_id)

    @app.post("/transfers/{transfer_id}/retry-unresolved")
    def retry_unresolved(transfer_id: int):
        transfer_or_404(transfer_id)
        worker.retry_unresolved(transfer_id)
        return back_to(transfer_id)

    @app.post("/transfers/{transfer_id}/cancel")
    def cancel(transfer_id: int):
        transfer_or_404(transfer_id)
        worker.cancel(transfer_id)
        return back_to(transfer_id)

    @app.post("/transfers/{transfer_id}/delete")
    def delete(transfer_id: int):
        transfer_or_404(transfer_id)
        worker.cancel(transfer_id)
        store.delete_transfer(transfer_id)
        return RedirectResponse("/", 303)

    @app.get("/transfers/{transfer_id}/export.csv")
    def export_csv(transfer_id: int):
        transfer = transfer_or_404(transfer_id)
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(["position", "status", "title", "artists", "album", "duration", "youtube_music_url", "score"])
        for row in store.tracks(transfer_id):
            out.writerow([
                row["position"] + 1, row["status"], row["title"], ", ".join(json.loads(row["artists"])),
                row["album"], fmt_duration(row["duration_ms"] / 1000),
                f"https://music.youtube.com/watch?v={row['video_id']}" if row["video_id"] else "",
                f"{row['score']:.2f}" if row["score"] is not None else "",
            ])
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in transfer["name"]).strip() or "playlist"
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
        )

    # -- track review (HTMX) --------------------------------------------------

    def track_row(request: Request, track_id: int):
        row = track_or_404(track_id)
        transfer = transfer_or_404(row["transfer_id"])
        return render(request, "partials/track_row.html", headers={"HX-Trigger": "counts-changed"},
                      t=row, transfer=transfer)

    @app.post("/tracks/{track_id}/choose", response_class=HTMLResponse)
    def choose(request: Request, track_id: int, candidate: str = Form(...)):
        track_or_404(track_id)
        try:
            cand = Candidate.from_dict(json.loads(candidate))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Bad candidate")
        worker.choose(track_id, cand)
        return track_row(request, track_id)

    @app.post("/tracks/{track_id}/skip", response_class=HTMLResponse)
    def skip(request: Request, track_id: int):
        track_or_404(track_id)
        worker.skip(track_id)
        return track_row(request, track_id)

    @app.post("/tracks/{track_id}/reset", response_class=HTMLResponse)
    def reset(request: Request, track_id: int):
        """Undo a manual decision: go back to the automatic result."""
        row = track_or_404(track_id)
        candidates = row_candidates(row)
        best = candidates[0] if candidates else None
        status = matching.status_for(best.score if best else None)
        video_id = best.video_id if best and status != "not_found" else None
        store.set_track_match(track_id, status, video_id, best.score if best else None, candidates)
        store.cache_match(row["cache_key"], status, video_id, best.score if best else None, candidates)
        return track_row(request, track_id)

    @app.get("/tracks/{track_id}/search", response_class=HTMLResponse)
    def search(request: Request, track_id: int, q: str = ""):
        row = track_or_404(track_id)
        track = row_track(row)
        q = q.strip() or " ".join([*(track.artists[:1]), track.title])
        try:
            results = matching.rank(track, app.state.yt.search(q, "songs", 8) + app.state.yt.search(q, "videos", 4))
            error = None
        except YTMError as exc:
            results, error = [], str(exc)
        return render(request, "partials/search_results.html", t=row, results=results, q=q, error=error)

    return app
