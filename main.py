from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pathlib import Path
import subprocess, uuid, os, time, hashlib, threading, re, json

app = FastAPI()

BASE = Path(__file__).parent
CLIPS_DIR = BASE / "clips"
CLIPS_DIR.mkdir(exist_ok=True)
JOBS_FILE = CLIPS_DIR / "jobs.json"


def _save_jobs() -> None:
    try:
        with _lock:
            done = {k: v for k, v in jobs.items() if v["status"] == "done"}
        JOBS_FILE.write_text(json.dumps(done))
    except Exception:
        pass


def _load_jobs() -> None:
    if not JOBS_FILE.exists():
        return
    try:
        data = json.loads(JOBS_FILE.read_text())
        cutoff = time.time() - 86400
        with _lock:
            for k, v in data.items():
                if v.get("created_at", 0) > cutoff and (CLIPS_DIR / v.get("filename", "")).exists():
                    jobs[k] = v
    except Exception:
        pass

CLIP_PASSWORD = os.environ.get("CLIP_PASSWORD", "clipme")
_TOKEN = hashlib.sha256(f"{CLIP_PASSWORD}:yt-clips".encode()).hexdigest()

jobs: dict = {}
_lock = threading.Lock()

_login_attempts: dict = {}  # ip -> [timestamp, ...]
_rate_lock = threading.Lock()
_RATE_WINDOW = 60   # seconds
_RATE_MAX = 5       # failed attempts before lockout


def _rate_check(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login, False if locked out."""
    now = time.time()
    with _rate_lock:
        hits = [t for t in _login_attempts.get(ip, []) if now - t < _RATE_WINDOW]
        _login_attempts[ip] = hits
        return len(hits) < _RATE_MAX


def _rate_record(ip: str) -> None:
    now = time.time()
    with _rate_lock:
        _login_attempts.setdefault(ip, []).append(now)


def authed(req: Request) -> bool:
    return req.cookies.get("auth") == _TOKEN


def hms(t: str) -> str:
    parts = t.strip().split(":")
    if len(parts) == 2:
        return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    if len(parts) == 3:
        return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2].zfill(2)}"
    raise ValueError(f"bad time format: {t!r} — use MM:SS or HH:MM:SS")


def _worker(job_id: str, url: str, start: str, end: str, quality: str):
    cmd = [
        "yt-dlp",
        "--download-sections", f"*{start}-{end}",
        "--force-keyframes-at-cuts",
        "-f", (
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={quality}]+bestaudio"
            f"/best[height<={quality}]"
        ),
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", str(CLIPS_DIR / f"{job_id}.%(ext)s"),
        url,
    ]

    with _lock:
        jobs[job_id].update(status="running", progress="Fetching info...", pct=0)

    try:
        title_proc = subprocess.run(
            ["yt-dlp", "--print", "title", "--no-playlist", url],
            capture_output=True, text=True, timeout=20
        )
        title = title_proc.stdout.strip() if title_proc.returncode == 0 else None
    except Exception:
        title = None

    with _lock:
        jobs[job_id]["title"] = title
        jobs[job_id]["progress"] = "Starting download..."

    phase = [0]  # 0=video, 1=audio
    log = []
    total_frames = [None]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            log.append(line)

            with _lock:
                cur_pct = jobs[job_id].get("pct", 0)

            pct = cur_pct
            progress = None  # None = don't update

            if "Downloading audio" in line:
                phase[0] = 1

            if "[download]" in line and "%" in line:
                m = re.search(r"(\d+\.?\d*)%", line)
                if m:
                    p = float(m.group(1))
                    if phase[0] == 0:
                        pct = p * 0.5
                        progress = f"Video {p:.0f}%"
                    else:
                        pct = 50 + p * 0.45
                        progress = f"Audio {p:.0f}%"
            elif "Merger" in line or "Merging" in line:
                pct = 97
                progress = "Merging video + audio..."
            elif line.startswith("frame="):
                # ffmpeg encoding progress: "frame=  123 fps= 45 ..."
                fm = re.search(r"frame=\s*(\d+)", line)
                dur_m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
                if fm and dur_m:
                    h, mn, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
                    encoded_s = h * 3600 + mn * 60 + s
                    # estimate total duration from first frame line if we have it
                    if total_frames[0] is None and encoded_s > 0:
                        total_frames[0] = encoded_s  # update as we go
                    if total_frames[0] and total_frames[0] > 0:
                        enc_pct = min(encoded_s / total_frames[0], 1.0) if total_frames[0] > 0 else 0
                        pct = 50 + enc_pct * 45
                    fps_m = re.search(r"fps=\s*(\d+)", line)
                    fps = fps_m.group(1) if fps_m else "?"
                    progress = f"Encoding... {fps} fps"
                    total_frames[0] = max(total_frames[0] or 0, encoded_s)
                else:
                    progress = "Encoding..."
                    pct = max(cur_pct, 50)
            elif "[ffmpeg]" in line and "Destination" not in line:
                pct = max(cur_pct, 50)
                progress = "Processing..."

            if progress is not None:
                with _lock:
                    jobs[job_id]["pct"] = pct
                    jobs[job_id]["progress"] = progress

        proc.wait()

        if proc.returncode == 0:
            found = list(CLIPS_DIR.glob(f"{job_id}*.mp4"))
            if found:
                with _lock:
                    jobs[job_id].update(status="done", filename=found[0].name, pct=100, progress="Done")
                _save_jobs()
            else:
                with _lock:
                    jobs[job_id].update(status="error", error="output file not found after download")
        else:
            with _lock:
                jobs[job_id].update(status="error", error="\n".join(log[-10:]))

    except Exception as e:
        with _lock:
            jobs[job_id].update(status="error", error=str(e))


def _cleanup():
    while True:
        time.sleep(3600)
        cutoff = time.time() - 86400
        with _lock:
            old = [k for k, v in jobs.items() if v["created_at"] < cutoff]
        for k in old:
            with _lock:
                j = jobs.pop(k, {})
            if j.get("filename"):
                (CLIPS_DIR / j["filename"]).unlink(missing_ok=True)
        if old:
            _save_jobs()


_load_jobs()

# Delete clip files on disk that have no matching job entry
def _cleanup_orphans() -> None:
    with _lock:
        known = {v["filename"] for v in jobs.values() if v.get("filename")}
    for f in CLIPS_DIR.glob("*.mp4"):
        if f.name not in known:
            f.unlink(missing_ok=True)

_cleanup_orphans()
threading.Thread(target=_cleanup, daemon=True).start()

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-clips</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:2rem;width:320px}
h1{font-size:1.4rem;font-weight:800;margin-bottom:1.75rem;letter-spacing:-.5px}
h1 em{color:#f97316;font-style:normal}
input{width:100%;padding:.75rem 1rem;background:#111;border:1px solid #333;border-radius:8px;color:#eee;font-size:1rem;margin-bottom:1rem;outline:none;transition:border-color .15s}
input:focus{border-color:#f97316}
.btn{width:100%;padding:.8rem;background:#f97316;border:none;border-radius:8px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:#ea6c00}
.err{color:#f97316;font-size:.82rem;margin-bottom:.75rem;display:none}
</style>
</head>
<body>
<div class="card">
  <h1>yt<em>-</em>clips</h1>
  <p class="err" id="err">wrong password</p>
  <input id="pw" type="password" placeholder="password" onkeydown="if(event.key==='Enter')go()">
  <button class="btn" onclick="go()">enter</button>
</div>
<script>
async function go(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  if(r.ok)location.href='/';
  else{document.getElementById('err').style.display='block';}
}
</script>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _LOGIN_HTML


@app.post("/api/login")
async def do_login(request: Request):
    ip = request.client.host
    if not _rate_check(ip):
        return JSONResponse({"ok": False, "error": "too many attempts"}, status_code=429)
    data = await request.json()
    if data.get("password") == CLIP_PASSWORD:
        r = JSONResponse({"ok": True})
        r.set_cookie("auth", _TOKEN, httponly=True, samesite="lax", max_age=86400 * 30)
        return r
    _rate_record(ip)
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not authed(request):
        return RedirectResponse("/login")
    return (BASE / "static" / "index.html").read_text()


@app.post("/api/clip")
async def create_clip(request: Request):
    if not authed(request):
        raise HTTPException(401)
    d = await request.json()
    url = d.get("url", "").strip()
    start_raw = d.get("start", "").strip()
    end_raw = d.get("end", "").strip()
    quality = str(d.get("quality", "1080"))

    if not all([url, start_raw, end_raw]):
        raise HTTPException(400, "url, start, and end are required")

    try:
        start, end = hms(start_raw), hms(end_raw)
    except ValueError as e:
        raise HTTPException(400, str(e))

    jid = str(uuid.uuid4())
    with _lock:
        jobs[jid] = {
            "status": "pending",
            "pct": 0,
            "progress": "Queued",
            "created_at": time.time(),
            "url": url,
            "start_raw": start_raw,
            "end_raw": end_raw,
        }

    threading.Thread(target=_worker, args=(jid, url, start, end, quality), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, request: Request):
    if not authed(request):
        raise HTTPException(401)
    with _lock:
        j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return {k: j.get(k) for k in ("status", "pct", "progress", "filename", "error")}


@app.get("/api/clips")
async def list_clips(request: Request):
    if not authed(request):
        raise HTTPException(401)
    with _lock:
        return [
            {
                "job_id": k,
                "filename": v.get("filename"),
                "title": v.get("title"),
                "start_raw": v.get("start_raw"),
                "end_raw": v.get("end_raw"),
                "url": v.get("url"),
                "created_at": v["created_at"],
            }
            for k, v in sorted(jobs.items(), key=lambda x: -x[1]["created_at"])
            if v["status"] == "done"
        ]


@app.delete("/api/clips")
async def clear_clips(request: Request):
    if not authed(request):
        raise HTTPException(401)
    with _lock:
        filenames = [v["filename"] for v in jobs.values() if v.get("filename")]
        jobs.clear()
    for fname in filenames:
        (CLIPS_DIR / fname).unlink(missing_ok=True)
    _save_jobs()
    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download(job_id: str, request: Request):
    if not authed(request):
        raise HTTPException(401)
    with _lock:
        j = jobs.get(job_id)
    if not j or j["status"] != "done":
        raise HTTPException(404)
    fp = CLIPS_DIR / j["filename"]
    if not fp.exists():
        raise HTTPException(404)
    return FileResponse(str(fp), media_type="video/mp4", filename=j["filename"])
