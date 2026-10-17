"""Read playlists exported by Exportify (https://exportify.app).

Exportify is an approved Spotify app, so it can read every playlist and the
Liked Songs of any account, Free included. It saves one CSV per playlist, or
all of them in a .zip. The column titles are translated to the user's
language, so columns are read by position, which Exportify keeps fixed:

    0 Track URI, 1 Track Name, 2 Artist URI(s), 3 Artist Name(s),
    4 Album URI, 5 Album Name, ... 12 Track Duration (ms), ... 16 ISRC
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .models import Track

URI, NAME, ARTIST_NAMES, ALBUM, DURATION, ISRC = 0, 1, 3, 5, 12, 16
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
_ARTIST_SEP = re.compile(r"(?<!\\), ")


class ExportifyError(Exception):
    pass


@dataclass
class ExportedPlaylist:
    name: str
    tracks: list[Track] = field(default_factory=list)


def split_artists(value: str) -> list[str]:
    # Exportify joins artists with ", " and writes commas inside a name as "\,".
    return [a.replace("\\,", ",").strip() for a in _ARTIST_SEP.split(value) if a.strip()]


def name_from_filename(filename: str) -> str:
    """Exportify file names are the playlist name, lowercased, with "_" for spaces."""
    stem = PurePosixPath(filename).stem
    if stem.lower() == "liked":  # Exportify's name for Liked Songs
        return "Liked Songs"
    name = re.sub(r"_+", " ", stem).strip()
    return name[:1].upper() + name[1:] if name else "Imported from Spotify"


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def parse_csv(text: str, filename: str) -> ExportedPlaylist:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or len(rows[0]) <= DURATION:
        raise ExportifyError(f"{filename} is not an Exportify CSV.")
    playlist = ExportedPlaylist(name_from_filename(filename))
    for row in rows[1:]:
        uri, title = _cell(row, URI), _cell(row, NAME)
        if not title:
            continue
        if uri and not uri.startswith("spotify:"):
            raise ExportifyError(f"{filename} is not an Exportify CSV.")
        try:
            duration = int(float(_cell(row, DURATION) or 0))
        except ValueError:
            duration = 0
        playlist.tracks.append(Track(
            title=title,
            artists=split_artists(_cell(row, ARTIST_NAMES)),
            album=_cell(row, ALBUM),
            duration_ms=duration,
            # spotify:local:... files have no track id
            spotify_id=uri.rsplit(":", 1)[1] if uri.startswith("spotify:track:") else None,
            isrc=_cell(row, ISRC) or None,
        ))
    return playlist


def parse_files(files: list[tuple[str, bytes]]) -> list[ExportedPlaylist]:
    """Playlists in uploaded .csv files and .zip archives of them."""
    playlists: list[ExportedPlaylist] = []
    for filename, data in files:
        if filename.lower().endswith(".zip"):
            try:
                archive = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile as exc:
                raise ExportifyError(f"{filename} is not a valid .zip file.") from exc
            members = [m for m in archive.infolist() if m.filename.lower().endswith(".csv") and not m.is_dir()]
            if sum(m.file_size for m in members) > MAX_UNZIPPED_BYTES:
                raise ExportifyError(f"{filename} is too large.")
            for member in sorted(members, key=lambda m: m.filename.lower()):
                playlists.append(parse_csv(archive.read(member).decode("utf-8", "replace"), member.filename))
        elif filename.lower().endswith(".csv"):
            playlists.append(parse_csv(data.decode("utf-8", "replace"), filename))
        else:
            raise ExportifyError(f"{filename}: upload the .csv or .zip files saved by Exportify.")
    return [p for p in playlists if p.tracks]
