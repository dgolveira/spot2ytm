"""Entry point: ``spot2ytm`` or ``python -m spot2ytm``."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from . import __version__, logs
from .config import Settings
from .web import create_app

log = logging.getLogger("spot2ytm")


def main() -> None:
    parser = argparse.ArgumentParser(prog="spot2ytm", description="Copy Spotify playlists to YouTube Music")
    parser.add_argument("--host", help="interface to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port to listen on (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser on start")
    parser.add_argument("--log-level", help="trace, debug, info, warn, error, fatal or silent (default info)")
    parser.add_argument("--log-file", help='also write logs to this file (default data/spot2ytm.log, "" for none)')
    args = parser.parse_args()

    settings = Settings.load()
    if args.host:
        settings.host = args.host
    if args.port and args.port != settings.port:
        settings.port = args.port
        settings.base_url = f"http://127.0.0.1:{args.port}"

    if args.log_level:
        settings.log_level = args.log_level
    if args.log_file is not None:
        settings.log_file = args.log_file
    try:
        logs.setup(settings.log_level, settings.log_file or None)
    except ValueError as exc:
        parser.error(str(exc))

    app = create_app(settings)
    log.info(
        "spot2ytm is running at %s", settings.base_url,
        extra={"version": __version__, "url": settings.base_url, "dataDir": str(settings.data_dir),
               "logFile": settings.log_file or None},
    )
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(settings.base_url,)).start()
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
