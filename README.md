# yt-clips

Download time-range clips from YouTube VODs in 1080p. Built for pulling volleyball highlights from full-game uploads without keeping the whole video locally.

> **vibe coded** with [Claude Code](https://claude.com/claude-code)

![screenshot placeholder](https://placehold.co/640x360/0f0f0f/f97316?text=yt-clips)

## Features

- Paste a YouTube URL, set start/end timestamps, get an mp4
- 1080p by default (720p / 480p available)
- Password-protected with rate limiting (5 failed attempts / 60s per IP)
- Clips auto-purge after 24 hours
- Video title shown in recent clips list
- Live encoding progress

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

Set a custom password via env var:

```bash
CLIP_PASSWORD=yourpassword uvicorn main:app --reload --port 8765
```

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
