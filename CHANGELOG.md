# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-06

First release.

### Added
- `mailrows send`: one templated email per row of an `.xlsx` or `.csv` file, with a preview
  table, a fully rendered sample, and a typed `yes` before anything is sent.
- `--dry-run` (changes nothing) and `--test-to ADDRESS` (everything to one address, `[TEST]`
  subject, no files or logs changed).
- Row checks: valid address, required fields, unique IDs. Bad rows are listed and skipped.
- Templates with `{column}` placeholders, a `Subject:` first line and an optional HTML version.
  Unknown placeholders stop the run before sending.
- SQLite send log keyed by row ID and message hash. Reruns never send twice, including after a
  crash or a cleared status column. `mailrows resolve` for emails whose outcome is unknown.
- Post-send hooks: Airtable (records checked before sending, under 5 calls per second, retries
  with backoff, "sent, update pending" state), webhook and CSV append.
- `mailrows smtp-check`: finds the working port and security setting without sending.
- `mailrows init`: a starter folder with a config, a sample sheet and templates.
- Settings from `config.ini` or `MAILROWS_*` environment variables.
- Double-click `.bat` helpers for Windows.
