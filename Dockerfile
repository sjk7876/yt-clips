FROM python:3.11-slim

# deno is yt-dlp's default JS runtime — required for YouTube signature
# deciphering. Without it, video downloads get throttled to ~nothing and
# clips come out audio-only.
COPY --from=denoland/deno:bin-2.9.6 /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# YouTube breaks yt-dlp constantly, so refresh it at build time (deploy.sh
# passes a new YTDLP_REFRESH each run) instead of on every container start.
ARG YTDLP_REFRESH=0
RUN pip install --no-cache-dir --upgrade yt-dlp

# uid 1000 matches the owner of ~/docker/yt-clips/clips on the host
RUN useradd --uid 1000 --create-home app

COPY main.py .
COPY static/ static/

RUN mkdir -p clips && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
