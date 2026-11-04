# yt-clips

FastAPI app that downloads time-range clips (and, for another project, full VODs) from YouTube via yt-dlp + ffmpeg. Live at https://clip.spenc.me.

## Layout

- `main.py` — the whole backend. Login/register/settings/admin pages are inline HTML strings in here.
- `static/index.html` — main clip UI, plain JS, no framework or build step.
- `clips/` — runtime data (gitignored): output `.mp4`s plus `jobs.json`, `users.json`, `settings.json`, `requests.json`. No database.

## Deploy

**Always deploy with `./deploy.sh`.** It's gitignored, so `git ls-files` won't show it. Don't hand-roll scp + `docker compose`: the script also rebuilds with a fresh yt-dlp (`YTDLP_REFRESH`), chowns `clips/` to uid 1000, makes sure `bgutil-provider` is up, and force-recreates the container.

The server copy at `server:/home/spenc/docker/yt-clips` is plain copied files, not a git checkout. The compose service lives in `/home/spenc/docker/services/docker-compose.yml`.

## Local dev

```bash
source .venv/bin/activate
ADMIN_PASSWORD=pw uvicorn main:app --reload --port 8765
```

HD downloads won't work locally without the bgutil PO-token sidecar (`POT_PROVIDER_URL`) and deno; see the comment above `_YTDLP_YT_ARGS`. There's no test suite. Verify with mocked `subprocess` calls, or by calling handlers directly with `_get_user` patched (the venv has no httpx for `TestClient`).

## How jobs work

- `POST /api/clip` and `POST /api/vod` both start `_worker` in a thread. Jobs live in the in-memory `jobs` dict guarded by `_lock`; only `done` jobs are saved to `jobs.json`.
- Clips use `--download-sections` + `--force-keyframes-at-cuts`, which forces an ffmpeg re-encode. VODs (`start`/`end` = `None`) skip both, so yt-dlp just remuxes the streams.
- `_has_video_stream` exists because YouTube throttling can make yt-dlp exit 0 with an audio-only file.

## VODs (`kind: "vod"`)

Full-game downloads that get pulled to spenc's PC, then deleted with `DELETE /api/vod/{id}` (returns 409 while still downloading).

- Hidden from `/api/clips` and `/api/clips/all` (filtered in `_clip_filter`).
- Excluded from the storage cap (`_storage_bytes`) and the oldest-first purge.
- Ignored by the clip delete routes. **Nothing removes a VOD except the VOD delete endpoint.**

Any new code that walks `jobs` needs to decide whether it should see VODs.

## Gotchas

- Startup `_cleanup_orphans()` deletes every `.mp4` in `clips/` that isn't in `jobs.json`. Don't import `main` in a second process against the live `clips/` dir: it will delete in-progress downloads.
- Validate every URL with `_valid_url` and always put `"--"` before it in yt-dlp argv; this guards against argument injection.
- Sessions are an HMAC of username + password hash (`_session_token`), so changing a password logs that user out everywhere.
- The README says yt-dlp updates on container start. That's stale: it updates at build time via `deploy.sh`.

## Commits

No `Co-Authored-By` or Claude session lines in commit messages or PRs.
