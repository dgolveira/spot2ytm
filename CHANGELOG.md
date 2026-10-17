# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
