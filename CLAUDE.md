# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install for development: `pip install -e .` (entry points `qobuz-dl` and `qdl` are defined in [setup.py](setup.py))
- Install runtime deps only: `pip install -r requirements.txt`
- Lint: `flake8 qobuz_dl` — config in [.flake8](.flake8) (line length 80, ignores E203/E266/E501, complexity 18, formatted with Black)
- Run the CLI from source: `python main.py` (there is no `qobuz_dl/__main__.py`; or just `qobuz-dl` once installed)
- Reset config / re-enter creds: `qobuz-dl -r`
- There is no test suite in this repo.

User config and the downloads-tracking SQLite DB live at `~/.config/qobuz-dl/` (Linux/macOS) or `%APPDATA%\qobuz-dl\` (Windows): `config.ini` and `qobuz_dl.db`. The CLI auto-creates `config.ini` on first run via [_reset_config in cli.py](qobuz_dl/cli.py#L29).

## Architecture

The package is a thin orchestration layer over Qobuz's web API. Flow: **CLI args + ini config → `QobuzDL` orchestrator → `qopy.Client` (signed API calls) → `downloader.Download` (per-track download + tag) → `metadata.tag_flac`/`tag_mp3`**. A spoofed `Bundle` provides the credentials needed to talk to the API at all.

### Entry and config wiring
- [qobuz_dl/cli.py](qobuz_dl/cli.py) — `main()` reads `config.ini`, parses argparse via [commands.py](qobuz_dl/commands.py), then constructs `QobuzDL` and dispatches one of four subcommands (`dl`, `fun`, `lucky`, `me`). CLI flags **override** config values; defaults come from config.
- [qobuz_dl/commands.py](qobuz_dl/commands.py) — argparse layout: top-level flags (`-r`, `-p`, `-sc`) plus four subparsers that all share `add_common_arg` (quality, directory, format strings, etc.).

### Orchestrator
[qobuz_dl/core.py](qobuz_dl/core.py) — `QobuzDL` is the public module-usage entry point too (see README "Module usage"). Key dispatchers:
- `handle_url(url)` — routes a Qobuz URL by type (album/track/artist/label/playlist) using a `possibles` dict that maps each type to a `client` method and an iterable key. Artists/labels/playlists fan out into multiple `download_from_id` calls under a per-collection subdirectory.
- `download_list_of_urls` — branches to `download_lastfm_pl` for last.fm URLs or `download_from_txt_file` for local files, otherwise `handle_url`.
- `lucky_mode` / `interactive` — both build URL lists via `search_by_type` and feed them back through `download_list_of_urls`.
- `download_from_id` retries indefinitely on `requests.exceptions.RequestException` (covers `IncompleteRead`, `ConnectionError`, timeouts, etc.) with a 5-minute wait between attempts. The wait is interruptible from stdin: typing `y` + Enter retries immediately. `NonStreamable` still skips without retry. The stdin wait uses `select` on Unix and `msvcrt` on Windows ([_wait_for_retry in core.py](qobuz_dl/core.py#L124)); on EOF (e.g., piped stdin) it sleeps out the remainder rather than busy-looping.

### Qobuz API client (the tricky part)
[qobuz_dl/qopy.py](qobuz_dl/qopy.py) — `Client` wraps `https://www.qobuz.com/api.json/0.2/`. Two non-obvious things:

1. **Auth uses a user_auth_token (UAT), not a password.** Direct email+password login was replaced upstream by OAuth+reCAPTCHA. The config field named `password` actually stores the UAT. `Client.auth()` POSTs to `user/login` with `extra=partner` and the existing UAT in the `X-User-Auth-Token` header to refresh it; the refreshed token is **written back to `config.ini` automatically** (see [qopy.py:152-162](qobuz_dl/qopy.py#L152-L162); path is `_CONFIG_FILE`, mirroring `cli.py`). When the token expires (HTTP 401), the user must grab a fresh one from browser DevTools → Network → `user/login` POST → Response → `user_auth_token`, then run `qobuz-dl -r` and paste it as the "password".
2. **Stream URL endpoints (`track/getFileUrl`, `favorite/getUserFavorites`) require an MD5-signed request** built from a per-call concatenation of params + a Unix timestamp + an "app secret". `cfg_setup()` brute-tries each candidate secret against `track/getFileUrl` and caches the working one in `self.sec`.

[qobuz_dl/bundle.py](qobuz_dl/bundle.py) — `Bundle` scrapes `https://play.qobuz.com/login` to find the JS bundle URL, downloads the bundle, and regex-extracts the `app_id` and a list of base64-encoded "secrets" keyed by timezone. This is fragile — if Qobuz changes their bundle, the regexes (`_APP_ID_REGEX`, `_SEED_TIMEZONE_REGEX`, `_INFO_EXTRAS_REGEX`, `_BUNDLE_URL_REGEX`) are the breakpoints. Both `app_id` and secrets are persisted into `config.ini` at first-run/reset.

### Download + tagging
[qobuz_dl/downloader.py](qobuz_dl/downloader.py) — `Download.download_release()` and `download_track()` write to a temp dotfile (`.NN.tmp`), then `_download_and_tag` calls `metadata.tag_flac`/`tag_mp3` and renames to the final formatted name. Quality fallback is governed by the API's `restrictions: FormatRestrictedByFormatAvailability` flag; `_clean_format_str` swaps in `DEFAULT_FORMATS` when the user's format string references `bit_depth`/`sampling_rate` for an MP3 download. Multi-disc albums get `Disc N/` subdirs. Each track download retries up to 5× with exponential backoff, re-fetching a fresh `getFileUrl` URL each time (stale CDN URLs get rejected), and sleeps 1s between tracks; album folder names are truncated to 128 chars.

[qobuz_dl/metadata.py](qobuz_dl/metadata.py) — uses mutagen (`FLAC` / `id3.ID3`); shared `ID3_LEGEND` maps friendly keys to ID3 frame classes for MP3. FLAC cover embed bails (logged) if cover > `FLAC_MAX_BLOCKSIZE` (16MB) — relevant when `--og-cover` is used.

### Quality codes
`5` = MP3 320, `6` = 16-bit/44.1, `7` = 24-bit ≤96kHz, `27` = 24-bit >96kHz. Defined in `QUALITIES` ([core.py:26](qobuz_dl/core.py#L26)) and validated in [qopy.py:100](qobuz_dl/qopy.py#L100).

### Favorites (`me` subcommand)
`QobuzDL.me_mode` pages through `favorite/getUserFavorites` (signed, 500 per page) for `albums` or `tracks`; favorite tracks go to `<directory>/Favorite Tracks/`.

### Downloads DB
[qobuz_dl/db.py](qobuz_dl/db.py) — single-table SQLite (`downloads(id TEXT UNIQUE)`). `--no-db` bypasses the check; `qobuz-dl -p` deletes the file. `QobuzDL.__init__` no-ops the DB when `downloads_db=None`.

## Notes for working in this code

- This is a personal fork of the unmaintained vitiko98/qobuz-dl. Remotes: `origin` = github.com/MAlanWine/qobuz-dl, `upstream` = vitiko98. Upstream PRs #185, #235, #236, #302, #303, #307, #308 are merged; #326 (downloader CDN retry only) and two date fixes from #331 are cherry-picked. The competing auth PRs (#282, #306, #326-auth, #331-OAuth, #334) were **not** merged — this fork uses its own UAT-refresh auth.
- `qobuz_dl/qopy.py.bak` is a backup file checked in — ignore it; the live file is `qopy.py`.
- The `IneligibleError` exception is defined in [exceptions.py](qobuz_dl/exceptions.py) but no longer raised or imported anywhere.
