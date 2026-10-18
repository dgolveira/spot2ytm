"""FastAPI application: pages, HTMX partials and OAuth callbacks."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, matching
from .config import Settings
from .db import ACTIVE_STATUSES, Store, row_candidates, row_track
from .exportify import ExportedPlaylist, ExportifyError, parse_files
from .jobs import Worker, new_transfer
from .logs import Timer
from .models import Candidate, Track
from .public import EMBED_TRACK_LIMIT, PublicSpotify, PublicSpotifyError, parse_link, track_ids
from .spotify import LIKED_SONGS_ID, SpotifyAuthError, SpotifyClient, SpotifyError, authorize_url, make_pkce_pair
from .ytm import BROWSERS, YTMError, YTMusicClient

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


def fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return "less than a minute left"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min left"
    return f"about {minutes // 60} h {minutes % 60:02d} min left"


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
    app.state.uploads = {}  # token -> playlists read from Exportify files, waiting for confirmation

    # The app holds your credentials and listens on localhost: refuse requests
    # whose Host is not ours (DNS rebinding) and cross-site form posts (CSRF).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    request_ids = iter(range(1, 1 << 62))

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # pino-http's "request completed" line. Static files and the progress
        # polling are logged at debug so info stays readable.
        timer = Timer()
        req = {"id": next(request_ids), "method": request.method, "url": str(request.url.path)}
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request errored", extra={"req": req, "responseTime": timer.ms})
            raise
        path = request.url.path
        quiet = path.startswith("/static/") or path.endswith(("/status", "/activity")) or path == "/partials/transfers"
        status = response.status_code
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else (
            logging.DEBUG if quiet else logging.INFO)
        log.log(level, "request completed",
                extra={"req": req, "res": {"statusCode": status}, "responseTime": timer.ms})
        return response

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
    templates.env.filters["eta"] = fmt_eta
    templates.env.filters["fromjson"] = json.loads
    templates.env.globals["version"] = __version__
    templates.env.globals["MATCH_THRESHOLD"] = matching.MATCH_THRESHOLD
    templates.env.globals["EMBED_TRACK_LIMIT"] = EMBED_TRACK_LIMIT
    templates.env.globals["privacy_options"] = PRIVACY_OPTIONS
    # No browser to read cookies from inside the container.
    templates.env.globals["in_docker"] = bool(os.environ.get("SPOT2YTM_IN_DOCKER"))

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

    def awaiting(summaries):
        """Transfers that finished searching and wait for the user to confirm."""
        return [(t, c) for t, c in summaries if t["status"] == "review" and c["to_add"]]

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
            transfers=(summaries := transfer_summaries()),
            awaiting=awaiting(summaries),
            active_statuses=ACTIVE_STATUSES,
        )

    @app.get("/partials/transfers", response_class=HTMLResponse)
    def transfers_partial(request: Request, awaiting_count: int = -1):
        summaries = transfer_summaries()
        headers = {}
        if awaiting_count >= 0 and len(awaiting(summaries)) != awaiting_count:
            headers["HX-Refresh"] = "true"  # a search finished: show it in the confirmation box
        return render(request, "partials/transfer_list.html", headers=headers, transfers=summaries,
                      awaiting=awaiting(summaries), active_statuses=ACTIVE_STATUSES)

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
        return render(request, "setup_ytmusic.html", error=None, headers_raw="", browsers=BROWSERS)

    @app.post("/setup/ytmusic", response_class=HTMLResponse)
    def save_ytmusic(request: Request, headers_raw: str = Form("")):
        try:
            app.state.yt.connect_with_headers(headers_raw)
        except YTMError as exc:
            return render(request, "setup_ytmusic.html", status_code=400, error=str(exc), headers_raw=headers_raw,
                          browsers=BROWSERS)
        return RedirectResponse("/", 303)

    @app.post("/setup/ytmusic/browser", response_class=HTMLResponse)
    def ytmusic_from_browser(request: Request, browser: str = Form("")):
        try:
            app.state.yt.connect_from_browser(browser)
        except YTMError as exc:
            return render(request, "setup_ytmusic.html", status_code=400, error=str(exc), headers_raw="",
                          browsers=BROWSERS, browser=browser)
        return RedirectResponse("/", 303)

    @app.post("/auth/ytmusic/disconnect")
    def ytmusic_disconnect():
        app.state.yt.disconnect()
        return RedirectResponse("/", 303)

    # -- playlists ------------------------------------------------------------

    # Every transfer stops after searching YouTube Music: nothing is written
    # there until the user has reviewed the matches and confirmed.

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
        return render(request, "playlists.html", playlists=items, last=store.last_transfer_by_playlist())

    def after_create(created: list[int]):
        if len(created) == 1:
            return RedirectResponse(f"/transfers/{created[0]}", 303)
        return RedirectResponse("/", 303)

    @app.post("/transfers")
    async def create_transfers(request: Request):
        form = await request.form()
        ids = [str(v) for v in form.getlist("playlist_id")]
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
            transfer_id = new_transfer(store, playlist)
            worker.enqueue(transfer_id)
            created.append(transfer_id)
        return after_create(created)

    # -- import without a Spotify login ----------------------------------------

    IMPORT_TABS = ("exportify", "link", "songs")

    def import_form(request: Request, tab: str = "exportify", status_code: int = 200, error: str | None = None,
                    **values):
        return render(request, "import.html", status_code=status_code, error=error,
                      tab=tab if tab in IMPORT_TABS else "exportify", values=values)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request, tab: str = "exportify"):
        return import_form(request, tab)

    def create_named_transfer(name: str, tracks: list[Track]) -> int:
        # Same name, same YouTube Music playlist: importing again only adds new songs.
        key = store.key_for_name(name) or "named:" + " ".join(name.lower().split())
        transfer_id = store.create_transfer(key, name, "", None, "PRIVATE", False)
        store.add_tracks(transfer_id, tracks)
        worker.enqueue(transfer_id)
        return transfer_id

    @app.post("/import/link", response_class=HTMLResponse)
    def import_link(request: Request, link: str = Form("")):
        found = parse_link(link)
        if not found or found[0] not in ("playlist", "album"):
            return import_form(request, "link", 400, "That is not a Spotify playlist or album link.", link=link)
        try:
            playlist, tracks = public.collection(*found)
        except PublicSpotifyError as exc:
            return import_form(request, "link", 400, str(exc), link=link)
        if not tracks:
            return import_form(request, "link", 400, "That playlist has no songs that can be read.", link=link)
        transfer_id = new_transfer(store, playlist)
        store.add_tracks(transfer_id, tracks)
        worker.enqueue(transfer_id)
        return after_create([transfer_id])

    @app.post("/import/songs", response_class=HTMLResponse)
    def import_songs(request: Request, songs: str = Form(""), name: str = Form("")):
        ids = track_ids(songs)
        name = name.strip()[:150]
        if not ids:
            return import_form(request, "songs", 400, "No Spotify song links found in what you pasted.",
                               songs=songs, name=name)
        if not name:
            return import_form(request, "songs", 400, "Give the playlist a name.", songs=songs, name=name)
        stubs = [Track(title="", artists=[], spotify_id=i) for i in ids]
        return after_create([create_named_transfer(name, stubs)])

    @app.post("/import/exportify", response_class=HTMLResponse)
    async def import_exportify(request: Request, files: list[UploadFile] = File(...)):
        try:
            playlists = parse_files([(f.filename or "playlist.csv", await f.read()) for f in files])
        except ExportifyError as exc:
            return import_form(request, "exportify", 400, str(exc))
        if not playlists:
            return import_form(request, "exportify", 400, "No songs found in those files.")
        token = secrets.token_urlsafe(16)
        app.state.uploads = {token: playlists}  # only the latest upload is kept
        return render(request, "import_exportify.html", token=token, playlists=playlists)

    @app.post("/import/exportify/confirm")
    async def import_exportify_confirm(request: Request):
        form = await request.form()
        playlists: list[ExportedPlaylist] | None = app.state.uploads.pop(str(form.get("token")), None)
        if playlists is None:
            return RedirectResponse("/import", 303)
        created = []
        for value in form.getlist("playlist_id"):
            try:
                playlist = playlists[int(str(value))]
            except (ValueError, IndexError):
                continue
            name = str(form.get(f"name_{value}") or "").strip()[:150] or playlist.name
            created.append(create_named_transfer(name, playlist.tracks))
        return after_create(created)

    # -- confirming -----------------------------------------------------------

    def confirm_push(transfer_id: int, privacy: str) -> None:
        transfer = store.get_transfer(transfer_id)
        if transfer is None or transfer["status"] in ACTIVE_STATUSES:
            return
        if privacy in PRIVACY_OPTIONS and not transfer["yt_playlist_id"]:
            store.update_transfer(transfer_id, privacy=privacy)
        worker.request(transfer_id, "push")

    @app.post("/transfers/push-many")
    async def push_many(request: Request):
        form = await request.form()
        if not app.state.yt.connected:
            return RedirectResponse("/setup/ytmusic", 303)
        privacy = str(form.get("privacy") or "")
        for value in form.getlist("playlist_id"):
            if str(value).isdigit():
                confirm_push(int(str(value)), privacy)
        return RedirectResponse("/", 303)

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
            activity=worker.activity(transfer_id),
            yt_url=YTMusicClient.playlist_url(transfer["yt_playlist_id"]) if transfer["yt_playlist_id"] else None,
        )

    @app.get("/transfers/{transfer_id}/activity", response_class=HTMLResponse)
    def transfer_activity(request: Request, transfer_id: int):
        transfer = transfer_or_404(transfer_id)
        if transfer["status"] not in ACTIVE_STATUSES:
            return HTMLResponse("", status_code=286)  # htmx: stop polling
        return render(request, "partials/transfer_activity.html",
                      transfer=transfer, activity=worker.activity(transfer_id))

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
    def push(transfer_id: int, privacy: str = Form("")):
        transfer_or_404(transfer_id)
        if not app.state.yt.connected:
            return RedirectResponse("/setup/ytmusic", 303)
        confirm_push(transfer_id, privacy)
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
