# yt-clips

Download time-range clips from YouTube VODs in 1080p. Built for pulling volleyball highlights from full-game uploads without keeping the whole video locally.

> **vibe coded** with [Claude Code](https://claude.com/claude-code)

![screenshot placeholder](https://placehold.co/640x360/0f0f0f/f97316?text=yt-clips)

## Features

- Paste a YouTube URL → embedded player loads instantly for scrubbing
- **Mark Start / Mark End** buttons capture the current playback time
- 1080p by default (720p / 480p available)
- Downloads save as `Video_Title_5m48s-6m03s.mp4`
- Multi-user accounts — each person has their own login
- **Mine** tab shows your clips; **All** tab shows everyone's (with owner label)
- `/admin` page to add and remove users (admin only)
- Rate limiting on login (5 failed attempts / 60s per IP)
- Clips auto-purge after 24 hours; delete-all button for manual cleanup
- Video title shown in recent clips list
- Live encoding progress
- yt-dlp auto-updates on every container start so YouTube changes don't break it

## Stack

- **Backend** — FastAPI + yt-dlp + ffmpeg
- **Frontend** — plain HTML/CSS/JS, no framework
- **Deploy** — Docker + Caddy (automatic HTTPS)

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8765
```

Open `http://localhost:8765` — default password is `clipme`.

Set the admin credentials via env vars (defaults to username `spenc`, password `clipme`):

```bash
ADMIN_USER=yourname ADMIN_PASSWORD=yourpassword uvicorn main:app --reload --port 8765
```

On first run, `users.json` is created in `clips/` with the admin account. Add other users via `/admin`.

`CLIP_PASSWORD` still works as a fallback for `ADMIN_PASSWORD` if you're migrating from a single-password setup.

## Deploy (Docker + Caddy)

Assumes a Caddy reverse proxy on the target host.

1. Copy files to the server and add the service to your `docker-compose.yml`:

```yaml
yt-clips:
  build: /path/to/yt-clips
  container_name: yt-clips
  restart: unless-stopped
  environment:
    - CLIP_PASSWORD=${CLIP_PASSWORD}
  volumes:
    - /path/to/yt-clips/clips:/app/clips
  networks:
    - proxy-network
```

2. Add a Caddyfile entry:

```
clip.yourdomain.com {
    reverse_proxy yt-clips:8000
}
```

3. Add `CLIP_PASSWORD` to your `.env`, then:

```bash
docker compose build yt-clips && docker compose up -d yt-clips
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Clips storage

Clips land in `./clips/` and are deleted after 24 hours. The directory is gitignored — mount it as a volume in production so it survives container rebuilds.
