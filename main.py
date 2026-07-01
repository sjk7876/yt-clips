from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pathlib import Path
import subprocess, uuid, os, time, hashlib, threading, re, json, hmac as _hmac

app = FastAPI()

BASE = Path(__file__).parent
CLIPS_DIR = BASE / "clips"
CLIPS_DIR.mkdir(exist_ok=True)
JOBS_FILE     = CLIPS_DIR / "jobs.json"
USERS_FILE    = CLIPS_DIR / "users.json"
SETTINGS_FILE = CLIPS_DIR / "settings.json"

# ── Config ────────────────────────────────────────────────────────────────────

ADMIN_USER     = os.environ.get("ADMIN_USER", "spenc")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or os.environ.get("CLIP_PASSWORD", "changeme")
_MASTER        = hashlib.sha256((os.environ.get("SECRET_KEY", ADMIN_PASSWORD) + ":yt-clips-master").encode()).hexdigest()

# ── Users ─────────────────────────────────────────────────────────────────────

users: dict = {}
_users_lock = threading.Lock()


def _hash_pw(username: str, password: str) -> str:
    return hashlib.sha256(f"{username}:{password}:yt-clips-pw".encode()).hexdigest()


def _session_token(username: str, pw_hash: str) -> str:
    return _hmac.new(_MASTER.encode(), f"{username}:{pw_hash}".encode(), hashlib.sha256).hexdigest()


def _load_users() -> None:
    if USERS_FILE.exists():
        try:
            with _users_lock:
                users.update(json.loads(USERS_FILE.read_text()))
            return
        except Exception:
            pass
    with _users_lock:
        users[ADMIN_USER] = {"password_hash": _hash_pw(ADMIN_USER, ADMIN_PASSWORD), "role": "admin"}
    _save_users()


def _save_users() -> None:
    try:
        with _users_lock:
            USERS_FILE.write_text(json.dumps(users, indent=2))
    except Exception:
        pass


def _get_user(request: Request):
    token = request.cookies.get("auth")
    if not token:
        return None
    with _users_lock:
        for uname, data in users.items():
            if _session_token(uname, data["password_hash"]) == token:
                return uname
    return None


def _get_role(username: str) -> str:
    with _users_lock:
        return users.get(username, {}).get("role", "user")


# ── Settings ─────────────────────────────────────────────────────────────────

settings: dict = {"storage_limit_gb": 20}


def _load_settings() -> None:
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            pass


def _save_settings() -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


def _storage_bytes() -> int:
    return sum(f.stat().st_size for f in CLIPS_DIR.glob("*.mp4") if f.exists())


def _enforce_storage_limit() -> None:
    limit = int(settings.get("storage_limit_gb", 20) * 1024 ** 3)
    used = _storage_bytes()
    if used <= limit:
        return
    with _lock:
        candidates = sorted(
            [(k, v) for k, v in jobs.items() if v["status"] == "done" and v.get("filename")],
            key=lambda x: x[1]["created_at"]
        )
    for jid, j in candidates:
        if used <= limit:
            break
        fp = CLIPS_DIR / j["filename"]
        if fp.exists():
            used -= fp.stat().st_size
            fp.unlink(missing_ok=True)
        with _lock:
            jobs.pop(jid, None)
    _save_jobs()


# ── Jobs ──────────────────────────────────────────────────────────────────────

jobs: dict = {}
_lock = threading.Lock()


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


# ── Rate limiting ─────────────────────────────────────────────────────────────

_login_attempts: dict = {}
_rate_lock = threading.Lock()
_RATE_WINDOW = 60
_RATE_MAX = 5


def _rate_check(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _login_attempts.get(ip, []) if now - t < _RATE_WINDOW]
        _login_attempts[ip] = hits
        return len(hits) < _RATE_MAX


def _rate_record(ip: str) -> None:
    with _rate_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


# ── Clip worker ───────────────────────────────────────────────────────────────

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
        r = subprocess.run(["yt-dlp", "--print", "title", "--no-playlist", url],
                           capture_output=True, text=True, timeout=20)
        title = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        title = None

    with _lock:
        jobs[job_id]["title"] = title
        jobs[job_id]["progress"] = "Starting download..."

    phase = [0]
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
            pct, progress = cur_pct, None
            if "Downloading audio" in line:
                phase[0] = 1
            if "[download]" in line and "%" in line:
                m = re.search(r"(\d+\.?\d*)%", line)
                if m:
                    p = float(m.group(1))
                    pct = p * 0.5 if phase[0] == 0 else 50 + p * 0.45
                    progress = f"Video {p:.0f}%" if phase[0] == 0 else f"Audio {p:.0f}%"
            elif "Merger" in line or "Merging" in line:
                pct, progress = 97, "Merging video + audio..."
            elif line.startswith("frame="):
                fps_m = re.search(r"fps=\s*(\d+)", line)
                fps = fps_m.group(1) if fps_m else "?"
                pct = max(cur_pct, 50)
                progress = f"Encoding... {fps} fps"
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
                _enforce_storage_limit()
            else:
                with _lock:
                    jobs[job_id].update(status="error", error="output file not found after download")
        else:
            with _lock:
                jobs[job_id].update(status="error", error="\n".join(log[-10:]))
    except Exception as e:
        with _lock:
            jobs[job_id].update(status="error", error=str(e))


# ── Cleanup ───────────────────────────────────────────────────────────────────

def _cleanup():
    """Hourly: remove job entries whose files have gone missing."""
    while True:
        time.sleep(3600)
        with _lock:
            orphaned = [k for k, v in jobs.items()
                        if v["status"] == "done" and v.get("filename")
                        and not (CLIPS_DIR / v["filename"]).exists()]
            for k in orphaned:
                jobs.pop(k, None)
        if orphaned:
            _save_jobs()


def _cleanup_orphans() -> None:
    with _lock:
        known = {v["filename"] for v in jobs.values() if v.get("filename")}
    for f in CLIPS_DIR.glob("*.mp4"):
        if f.name not in known:
            f.unlink(missing_ok=True)


# ── Startup ───────────────────────────────────────────────────────────────────

_load_settings()
_load_users()
_load_jobs()
_cleanup_orphans()
threading.Thread(target=_cleanup, daemon=True).start()


# ── Inline HTML ───────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-clips</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:2rem;width:340px}
h1{font-size:1.4rem;font-weight:800;margin-bottom:1.75rem;letter-spacing:-.5px}
h1 em{color:#f97316;font-style:normal}
label{display:block;font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.6px;margin-bottom:.35rem}
input{width:100%;padding:.7rem 1rem;background:#111;border:1px solid #2e2e2e;border-radius:8px;color:#eee;font-size:1rem;margin-bottom:1rem;outline:none;transition:border-color .15s}
input:focus{border-color:#f97316}
.btn{width:100%;padding:.8rem;background:#f97316;border:none;border-radius:8px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:#ea6c00}
.err{color:#e63946;font-size:.82rem;margin-bottom:.75rem;display:none}
</style>
</head>
<body>
<div class="card">
  <h1>yt<em>-</em>clips</h1>
  <p class="err" id="err">wrong username or password</p>
  <label>Username</label>
  <input id="un" type="text" placeholder="username" onkeydown="if(event.key==='Enter')go()">
  <label>Password</label>
  <input id="pw" type="password" placeholder="password" onkeydown="if(event.key==='Enter')go()">
  <button class="btn" onclick="go()">enter</button>
</div>
<script>
async function go(){
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:document.getElementById('un').value,password:document.getElementById('pw').value})});
  if(r.ok)location.href='/';
  else document.getElementById('err').style.display='block';
}
</script>
</body></html>"""


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-clips · admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:2.5rem 1rem}
header{width:100%;max-width:520px;display:flex;align-items:baseline;gap:.75rem;margin-bottom:2rem}
h1{font-size:1.4rem;font-weight:800;letter-spacing:-.5px}
h1 em{color:#f97316;font-style:normal}
.back{font-size:.8rem;color:#555;text-decoration:none}
.back:hover{color:#999}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:1.75rem;width:100%;max-width:520px;margin-bottom:1.25rem}
.card-title{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#444;margin-bottom:1.25rem}
.user-row{display:flex;align-items:center;gap:.75rem;padding:.7rem .9rem;background:#111;border:1px solid #222;border-radius:8px;margin-bottom:.5rem}
.user-name{flex:1;font-weight:600;font-size:.9rem}
.badge{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.5px;padding:.2rem .5rem;border-radius:4px;background:#2a2a2a;color:#666}
.badge.admin{background:#f9731622;color:#f97316}
.btn-del{padding:.35rem .75rem;background:transparent;border:1px solid #333;border-radius:6px;color:#666;font-size:.75rem;font-weight:600;cursor:pointer;transition:all .15s}
.btn-del:hover{border-color:#e63946;color:#e63946}
label{display:block;font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.6px;margin-bottom:.35rem}
input,select{width:100%;padding:.65rem .9rem;background:#111;border:1px solid #2e2e2e;border-radius:8px;color:#eee;font-size:.9rem;margin-bottom:.9rem;outline:none;transition:border-color .15s;-webkit-appearance:none}
input:focus,select:focus{border-color:#f97316}
select{cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23666' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:1.75rem}
.btn{width:100%;padding:.75rem;background:#f97316;border:none;border-radius:8px;color:#fff;font-size:.9rem;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:#ea6c00}
.msg{font-size:.8rem;margin-top:.75rem;text-align:center;display:none}
.msg.ok{color:#2ecc71}.msg.err{color:#e63946}
.storage-bar-bg{height:8px;background:#222;border-radius:99px;overflow:hidden;margin-bottom:.6rem}
.storage-bar-fill{height:100%;background:#f97316;border-radius:99px;transition:width .4s;width:0%}
.storage-bar-fill.warn{background:#e63946}
.storage-label{font-size:.82rem;color:#666}
</style>
</head>
<body>
<header>
  <h1>yt<em>-</em>clips</h1>
  <a class="back" href="/">← back</a>
</header>

<div class="card">
  <div class="card-title">Users</div>
  <div id="user-list">loading...</div>
</div>

<div class="card">
  <div class="card-title">Add User</div>
  <label>Username</label>
  <input id="new-un" type="text" placeholder="username">
  <label>Password</label>
  <input id="new-pw" type="password" placeholder="password">
  <label>Role</label>
  <select id="new-role">
    <option value="user">user</option>
    <option value="admin">admin</option>
  </select>
  <button class="btn" onclick="addUser()">Add User</button>
  <div class="msg" id="add-msg"></div>
</div>

<div class="card">
  <div class="card-title">Storage</div>
  <div class="storage-bar-bg"><div class="storage-bar-fill" id="stor-fill"></div></div>
  <div class="storage-label" id="stor-label">loading...</div>
  <div style="height:1.25rem"></div>
  <label>Storage Limit (GB)</label>
  <input id="stor-limit" type="number" min="1" step="1" placeholder="20">
  <button class="btn" onclick="saveLimit()">Save Limit</button>
  <div class="msg" id="stor-msg"></div>
</div>

<script>
async function loadUsers() {
  const r = await fetch('/api/admin/users');
  if (!r.ok) return;
  const list = await r.json();
  const el = document.getElementById('user-list');
  el.innerHTML = list.map(u => `
    <div class="user-row">
      <span class="user-name">${esc(u.username)}</span>
      <span class="badge ${u.role}">${esc(u.role)}</span>
      <button class="btn-del" onclick="delUser('${esc(u.username)}')">remove</button>
    </div>`).join('') || '<div style="color:#444;font-size:.85rem;padding:.5rem">no users</div>';
}

async function delUser(username) {
  if (!confirm('Remove ' + username + '?')) return;
  await fetch('/api/admin/users/' + username, {method:'DELETE'});
  loadUsers();
}

async function addUser() {
  const un = document.getElementById('new-un').value.trim();
  const pw = document.getElementById('new-pw').value;
  const role = document.getElementById('new-role').value;
  const msg = document.getElementById('add-msg');
  const r = await fetch('/api/admin/users', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username:un, password:pw, role})
  });
  if (r.ok) {
    msg.textContent = 'user added'; msg.className='msg ok'; msg.style.display='block';
    document.getElementById('new-un').value='';
    document.getElementById('new-pw').value='';
    loadUsers();
  } else {
    const e = await r.json().catch(()=>({}));
    msg.textContent = e.detail || 'error'; msg.className='msg err'; msg.style.display='block';
  }
  setTimeout(()=>msg.style.display='none', 3000);
}

async function loadStorage() {
  const r = await fetch('/api/admin/storage');
  if (!r.ok) return;
  const d = await r.json();
  const pct = Math.min(d.used_gb / d.limit_gb * 100, 100);
  const fill = document.getElementById('stor-fill');
  fill.style.width = pct + '%';
  fill.className = 'storage-bar-fill' + (pct > 85 ? ' warn' : '');
  document.getElementById('stor-label').textContent =
    `${d.used_gb} GB used of ${d.limit_gb} GB · ${d.clip_count} clip${d.clip_count !== 1 ? 's' : ''}`;
  document.getElementById('stor-limit').value = d.limit_gb;
}

async function saveLimit() {
  const val = parseFloat(document.getElementById('stor-limit').value);
  const msg = document.getElementById('stor-msg');
  const r = await fetch('/api/admin/settings', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({storage_limit_gb: val})
  });
  if (r.ok) {
    msg.textContent = 'saved'; msg.className = 'msg ok'; msg.style.display = 'block';
    loadStorage();
  } else {
    msg.textContent = 'error'; msg.className = 'msg err'; msg.style.display = 'block';
  }
  setTimeout(() => msg.style.display = 'none', 2000);
}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
loadUsers();
loadStorage();
</script>
</body></html>"""


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _LOGIN_HTML


@app.post("/api/login")
async def do_login(request: Request):
    ip = request.client.host
    if not _rate_check(ip):
        return JSONResponse({"ok": False, "error": "too many attempts"}, status_code=429)
    data = await request.json()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    with _users_lock:
        user_data = users.get(username)
    if not user_data or _hash_pw(username, password) != user_data["password_hash"]:
        _rate_record(ip)
        return JSONResponse({"ok": False}, status_code=401)
    r = JSONResponse({"ok": True})
    r.set_cookie("auth", _session_token(username, user_data["password_hash"]),
                 httponly=True, samesite="lax", max_age=86400 * 30)
    return r


@app.post("/api/logout")
async def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie("auth")
    return r


@app.get("/api/me")
async def me(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    return {"username": username, "role": _get_role(username)}


# ── Main app ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _get_user(request):
        return RedirectResponse("/login")
    return (BASE / "static" / "index.html").read_text()


@app.post("/api/clip")
async def create_clip(request: Request):
    username = _get_user(request)
    if not username:
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
            "status": "pending", "pct": 0, "progress": "Queued",
            "created_at": time.time(), "url": url,
            "start_raw": start_raw, "end_raw": end_raw, "owner": username,
        }
    threading.Thread(target=_worker, args=(jid, url, start, end, quality), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, request: Request):
    if not _get_user(request):
        raise HTTPException(401)
    with _lock:
        j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return {k: j.get(k) for k in ("status", "pct", "progress", "filename", "error")}


@app.get("/api/clips")
async def list_clips(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    is_admin = _get_role(username) == "admin"
    with _lock:
        return [
            {"job_id": k, "title": v.get("title"), "start_raw": v.get("start_raw"),
             "end_raw": v.get("end_raw"), "url": v.get("url"), "created_at": v["created_at"]}
            for k, v in sorted(jobs.items(), key=lambda x: -x[1]["created_at"])
            if v["status"] == "done" and (
                v.get("owner") == username or (is_admin and v.get("owner") is None)
            )
        ]


@app.get("/api/clips/all")
async def list_all_clips(request: Request):
    if not _get_user(request):
        raise HTTPException(401)
    with _lock:
        return [
            {"job_id": k, "title": v.get("title"), "start_raw": v.get("start_raw"),
             "end_raw": v.get("end_raw"), "url": v.get("url"),
             "created_at": v["created_at"], "owner": v.get("owner")}
            for k, v in sorted(jobs.items(), key=lambda x: -x[1]["created_at"])
            if v["status"] == "done"
        ]


@app.delete("/api/clips")
async def clear_clips(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    is_admin = _get_role(username) == "admin"
    with _lock:
        to_del = [k for k, v in jobs.items()
                  if v.get("owner") == username or (is_admin and v.get("owner") is None)]
        filenames = [jobs[k].get("filename") for k in to_del if jobs[k].get("filename")]
        for k in to_del:
            del jobs[k]
    for f in filenames:
        (CLIPS_DIR / f).unlink(missing_ok=True)
    _save_jobs()
    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download(job_id: str, request: Request):
    if not _get_user(request):
        raise HTTPException(401)
    with _lock:
        j = jobs.get(job_id)
    if not j or j["status"] != "done":
        raise HTTPException(404)
    fp = CLIPS_DIR / j["filename"]
    if not fp.exists():
        raise HTTPException(404)

    def _slug(s: str) -> str:
        return re.sub(r'[^\w\s-]', '', s).strip().replace(' ', '_')[:60]

    def _ts(t: str) -> str:
        parts = t.split(':')
        return (''.join(f"{p}{'hms'[i]}" for i, p in enumerate(parts)) if len(parts) == 3
                else f"{parts[0]}m{parts[1]}s") if ':' in t else t

    title = _slug(j.get('title') or 'clip')
    dl_name = f"{title}_{_ts(j.get('start_raw',''))}-{_ts(j.get('end_raw',''))}.mp4"
    return FileResponse(str(fp), media_type="video/mp4", filename=dl_name)


# ── Settings / storage API ───────────────────────────────────────────────────

@app.get("/api/admin/storage")
async def admin_storage(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    used = _storage_bytes()
    limit = int(settings.get("storage_limit_gb", 20) * 1024 ** 3)
    with _lock:
        clip_count = sum(1 for v in jobs.values() if v["status"] == "done")
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "used_gb": round(used / 1024 ** 3, 2),
        "limit_gb": settings.get("storage_limit_gb", 20),
        "clip_count": clip_count,
    }


@app.put("/api/admin/settings")
async def update_settings(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    data = await request.json()
    if "storage_limit_gb" in data:
        val = float(data["storage_limit_gb"])
        if val <= 0:
            raise HTTPException(400, "limit must be > 0")
        settings["storage_limit_gb"] = val
        _save_settings()
        _enforce_storage_limit()
    return {"ok": True, "settings": settings}


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        return RedirectResponse("/")
    return _ADMIN_HTML


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    with _users_lock:
        return [{"username": k, "role": v["role"]} for k, v in users.items()]


@app.post("/api/admin/users")
async def admin_add_user(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    data = await request.json()
    username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    role = data.get("role", "user")
    if not username or not password:
        raise HTTPException(400, "username and password required")
    with _users_lock:
        if username in users:
            raise HTTPException(409, "user already exists")
        users[username] = {"password_hash": _hash_pw(username, password), "role": role}
    _save_users()
    return {"ok": True}


@app.delete("/api/admin/users/{target}")
async def admin_delete_user(target: str, request: Request):
    me_user = _get_user(request)
    if _get_role(me_user or "") != "admin":
        raise HTTPException(403)
    if target == me_user:
        raise HTTPException(400, "cannot delete yourself")
    with _users_lock:
        if target not in users:
            raise HTTPException(404)
        del users[target]
    _save_users()
    return {"ok": True}
