# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- New look: warm monochrome palette, Instrument Serif headings with Geist Sans and Geist Mono
  (bundled, no external requests), flat 1px-bordered surfaces, black primary buttons, pastel
  status tags and subtle entry motion. Fixed horizontal scrolling on phones.

- Logs are JSON lines in pino's format (levels, fields and error serializer), on stdout and in
  `data/spot2ytm.log`, with every request and every matched song. Set the level with
  `SPOT2YTM_LOG_LEVEL`/`LOG_LEVEL` or `--log-level`.
- Docker: log rotation and a health check in `docker-compose.yml`.

- Nothing is written to YouTube Music before you confirm: every transfer stops after the search,
  shows what was found and waits. The dashboard lists the playlists waiting for confirmation, to
  create several at once, and privacy is chosen at that step.
- Import page reorganized in tabs: Exportify file, playlist link, song links.

### Added

- Spotify can be disconnected from every state of the dashboard card, and "Remove app" forgets the
  developer app (client ID) to start over. When Spotify refuses the app because its owner has no
  Premium (403 "Active premium subscription required"), the dashboard and the playlists page say so
  and point to the Exportify import, instead of showing the raw API error.
- Live view on the transfer page: the song being searched and the exact search being tried
  (ISRC, title and artist, videos), the last songs with what was found and the confidence,
  speed and time left, a progress bar split by result and a level-meter animation.
- Import without a Spotify login, for Spotify Free accounts: paste a public playlist or album link
  (first 100 songs) or the song links copied from the Spotify app (no limit, works for Liked Songs).
- Import the .csv/.zip files saved by Exportify (every playlist and Liked Songs, on Spotify Free too),
  with a screen to pick the playlists and fix their names.
- Search YouTube Music by ISRC first when it is known, which finds the exact recording.
- Connect YouTube Music automatically by reading the YouTube cookies of a browser on this machine,
  instead of pasting request headers by hand.

## [0.1.0] - 2026-10-17

### Added

- Web UI to connect Spotify (OAuth PKCE, read-only) and YouTube Music (browser headers).
- Playlist picker with Liked Songs support, filter and multi-select.
- Matching engine scoring title, artists, duration and version (live, remix, sped up…).
- Review screen: accept uncertain matches, pick another candidate, search manually or skip.
- Background worker with progress, cancel and automatic resume after restarts.
- Re-exporting a playlist only adds new songs to the same YouTube Music playlist.
- Match cache, so songs found once (or fixed by hand) are never searched again.
- CSV export of every transfer.
- Docker image and Compose file.
- CI with tests, CodeQL, pip-audit and Dependabot.

[Unreleased]: https://github.com/dgolveira/spot2ytm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dgolveira/spot2ytm/releases/tag/v0.1.0
