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

COPY main.py .
COPY static/ static/

RUN mkdir -p clips

EXPOSE 8000

CMD ["sh", "-c", "pip install -q --upgrade yt-dlp && uvicorn main:app --host 0.0.0.0 --port 8000"]
