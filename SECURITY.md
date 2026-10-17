# Security policy

## Supported versions

Only the latest release on the `main` branch receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: go to the
[Security tab](https://github.com/dgolveira/spot2ytm/security/advisories/new)
and click **Report a vulnerability**. Include the steps to reproduce, the impact
you expect and, if you have one, a suggested fix.

You can expect a first answer within a week. Once a fix is released you will be
credited in the release notes, unless you prefer to stay anonymous.

## How spot2ytm handles your credentials

spot2ytm is meant to run **on your own machine**, for **one user**:

- It listens on `127.0.0.1` only by default and rejects requests whose `Host`
  header is not a known local name (protection against DNS rebinding).
- Cross-site `POST` requests are refused (CSRF protection based on `Origin`/`Referer`).
- Spotify access uses OAuth with PKCE and **read-only scopes**. No client secret is stored.
- The YouTube Music browser headers, the Spotify token and the local database
  live in the `data/` folder, written with `0600` permissions. Treat that folder
  like a password: never commit it, never share it.
- Credentials are only ever sent to `accounts.spotify.com`, `api.spotify.com`
  and `music.youtube.com`.

**Do not expose spot2ytm to the internet.** It has no login of its own: anyone
who can reach it can act on the connected accounts.

## Automated checks

Every push and pull request runs:

- **CodeQL** static analysis (Python and JavaScript)
- **pip-audit** against the PyPI advisory database
- **Dependabot** alerts and weekly update PRs for Python packages, GitHub Actions and the Docker base image
