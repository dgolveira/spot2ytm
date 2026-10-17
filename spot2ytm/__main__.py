"""Entry point: ``spot2ytm`` or ``python -m spot2ytm``."""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from .config import Settings
from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="spot2ytm", description="Copy Spotify playlists to YouTube Music")
    parser.add_argument("--host", help="interface to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port to listen on (default 8000)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser on start")
    args = parser.parse_args()

    settings = Settings.load()
    if args.host:
        settings.host = args.host
    if args.port and args.port != settings.port:
        settings.port = args.port
        settings.base_url = f"http://127.0.0.1:{args.port}"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(settings)
    print(f"\n  spot2ytm is running at {settings.base_url}\n  (data stored in {settings.data_dir})\n")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(settings.base_url,)).start()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
