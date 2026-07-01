from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from pathlib import Path
import subprocess, uuid, os, time, hashlib, threading, re, json, hmac as _hmac

app = FastAPI()

BASE = Path(__file__).parent
CLIPS_DIR = BASE / "clips"
CLIPS_DIR.mkdir(exist_ok=True)
JOBS_FILE      = CLIPS_DIR / "jobs.json"
USERS_FILE     = CLIPS_DIR / "users.json"
SETTINGS_FILE  = CLIPS_DIR / "settings.json"
REQUESTS_FILE  = CLIPS_DIR / "requests.json"

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


# ── Registration requests ─────────────────────────────────────────────────────

reg_requests: list = []
_req_lock = threading.Lock()


def _load_requests() -> None:
    if REQUESTS_FILE.exists():
        try:
            with _req_lock:
                reg_requests.extend(json.loads(REQUESTS_FILE.read_text()))
        except Exception:
            pass


def _save_requests() -> None:
    try:
        with _req_lock:
            REQUESTS_FILE.write_text(json.dumps(reg_requests, indent=2))
    except Exception:
        pass


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
_load_requests()
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
  <p style="text-align:center;margin-top:1.25rem;font-size:.8rem"><a href="/register" style="color:#555;text-decoration:none">request account →</a></p>
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
.req-row{display:flex;align-items:center;gap:.75rem;padding:.7rem .9rem;background:#111;border:1px solid #222;border-radius:8px;margin-bottom:.5rem}
.req-name{flex:1}
.req-name strong{font-weight:600;font-size:.9rem}
.req-name span{font-size:.75rem;color:#555;display:block;margin-top:.1rem}
.btn-approve{padding:.35rem .75rem;background:#f9731622;border:1px solid #f97316;border-radius:6px;color:#f97316;font-size:.75rem;font-weight:600;cursor:pointer;transition:all .15s}
.btn-approve:hover{background:#f97316;color:#fff}
.btn-reject{padding:.35rem .75rem;background:transparent;border:1px solid #333;border-radius:6px;color:#666;font-size:.75rem;font-weight:600;cursor:pointer;transition:all .15s;margin-left:.25rem}
.btn-reject:hover{border-color:#e63946;color:#e63946}
</style>
</head>
<body>
<header>
  <h1>yt<em>-</em>clips</h1>
  <a class="back" href="/">← back</a>
</header>

<div class="card">
  <div class="card-title">Pending Requests <span id="req-badge" style="display:none;background:#f97316;color:#fff;font-size:.65rem;padding:.1rem .45rem;border-radius:4px;margin-left:.35rem;vertical-align:middle;font-weight:700"></span></div>
  <div id="req-list"></div>
</div>

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

async function loadRequests() {
  const r = await fetch('/api/admin/requests');
  if (!r.ok) return;
  const list = await r.json();
  const el = document.getElementById('req-list');
  const badge = document.getElementById('req-badge');
  if (!list.length) {
    el.innerHTML = '<div style="color:#444;font-size:.85rem;padding:.5rem">no pending requests</div>';
    badge.style.display = 'none';
  } else {
    badge.textContent = list.length;
    badge.style.display = 'inline';
    el.innerHTML = list.map(req => `
      <div class="req-row">
        <div class="req-name">
          <strong>${esc(req.username)}</strong>
          ${req.display_name ? `<span>${esc(req.display_name)}</span>` : ''}
        </div>
        <button class="btn-approve" onclick="approveReq('${esc(req.id)}')">approve</button>
        <button class="btn-reject" onclick="rejectReq('${esc(req.id)}')">reject</button>
      </div>`).join('');
  }
}

async function approveReq(id) {
  await fetch('/api/admin/requests/' + id + '/approve', {method:'POST'});
  loadRequests(); loadUsers();
}

async function rejectReq(id) {
  await fetch('/api/admin/requests/' + id, {method:'DELETE'});
  loadRequests();
}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
loadRequests();
loadUsers();
loadStorage();
</script>
</body></html>"""


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-clips · settings</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:system-ui,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:2.5rem 1rem}
header{width:100%;max-width:480px;display:flex;align-items:baseline;gap:.75rem;margin-bottom:2rem}
h1{font-size:1.4rem;font-weight:800;letter-spacing:-.5px}
h1 em{color:#f97316;font-style:normal}
.back{font-size:.8rem;color:#555;text-decoration:none}
.back:hover{color:#999}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:1.75rem;width:100%;max-width:480px;margin-bottom:1.25rem}
.card-title{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#444;margin-bottom:1.25rem}
label{display:block;font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.6px;margin-bottom:.35rem}
input{width:100%;padding:.65rem .9rem;background:#111;border:1px solid #2e2e2e;border-radius:8px;color:#eee;font-size:.9rem;margin-bottom:.9rem;outline:none;transition:border-color .15s}
input:focus{border-color:#f97316}
input[readonly]{color:#555;cursor:default}
.btn{width:100%;padding:.75rem;background:#f97316;border:none;border-radius:8px;color:#fff;font-size:.9rem;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:#ea6c00}
.msg{font-size:.8rem;margin-top:.75rem;text-align:center;display:none}
.msg.ok{color:#2ecc71}.msg.err{color:#e63946}
.note{font-size:.78rem;color:#555;margin-bottom:.9rem;line-height:1.5}
</style>
</head>
<body>
<header>
  <h1>yt<em>-</em>clips</h1>
  <a class="back" href="/">← back</a>
</header>

<div class="card">
  <div class="card-title">Display Name</div>
  <p class="note">shown as your name in the All Clips view. leave blank to show your username.</p>
  <label>Display Name</label>
  <input id="display-name" type="text" placeholder="e.g. Spencer">
  <button class="btn" onclick="saveDisplayName()">Save Display Name</button>
  <div class="msg" id="dn-msg"></div>
</div>

<div class="card">
  <div class="card-title">Username</div>
  <p class="note">changing your username will log you out — you'll need to sign back in.</p>
  <label>Current Username</label>
  <input id="cur-un" type="text" readonly>
  <label>New Username</label>
  <input id="new-un" type="text" placeholder="letters, numbers, hyphens, underscores">
  <label>Current Password (to confirm)</label>
  <input id="un-pw" type="password" placeholder="your current password">
  <button class="btn" onclick="changeUsername()">Save Username</button>
  <div class="msg" id="un-msg"></div>
</div>

<div class="card">
  <div class="card-title">Password</div>
  <label>Current Password</label>
  <input id="old-pw" type="password" placeholder="current password">
  <label>New Password</label>
  <input id="new-pw" type="password" placeholder="new password">
  <label>Confirm New Password</label>
  <input id="conf-pw" type="password" placeholder="confirm new password" onkeydown="if(event.key==='Enter')changePassword()">
  <button class="btn" onclick="changePassword()">Save Password</button>
  <div class="msg" id="pw-msg"></div>
</div>

<script>
async function loadMe() {
  const r = await fetch('/api/me');
  if (!r.ok) { location.href='/login'; return; }
  const d = await r.json();
  document.getElementById('cur-un').value = d.username;
  document.getElementById('new-un').value = d.username;
  document.getElementById('display-name').value = d.display_name || '';
}

async function saveDisplayName() {
  const val = document.getElementById('display-name').value.trim();
  const r = await fetch('/api/me/display_name', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({display_name: val})
  });
  showMsg('dn-msg', r.ok ? 'saved' : 'error', r.ok ? 'ok' : 'err');
}

async function changeUsername() {
  const newUn = document.getElementById('new-un').value.trim().toLowerCase();
  const pw    = document.getElementById('un-pw').value;
  const curUn = document.getElementById('cur-un').value;
  if (!newUn || !pw) { showMsg('un-msg', 'all fields required', 'err'); return; }
  if (newUn === curUn) { showMsg('un-msg', 'that is already your username', 'err'); return; }
  const r = await fetch('/api/me/username', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: newUn, password: pw})
  });
  const d = await r.json().catch(()=>({}));
  if (r.ok && d.ok) {
    showMsg('un-msg', 'updated — signing you out', 'ok');
    setTimeout(()=>{ location.href='/login'; }, 1500);
  } else {
    showMsg('un-msg', d.error || d.detail || 'error', 'err');
  }
}

async function changePassword() {
  const old = document.getElementById('old-pw').value;
  const nw  = document.getElementById('new-pw').value;
  const cf  = document.getElementById('conf-pw').value;
  if (!old || !nw) { showMsg('pw-msg', 'all fields required', 'err'); return; }
  if (nw !== cf)   { showMsg('pw-msg', "passwords don't match", 'err'); return; }
  const r = await fetch('/api/me/password', {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({current_password: old, new_password: nw})
  });
  const d = await r.json().catch(()=>({}));
  if (r.ok && d.ok) {
    showMsg('pw-msg', 'password updated', 'ok');
    document.getElementById('old-pw').value='';
    document.getElementById('new-pw').value='';
    document.getElementById('conf-pw').value='';
  } else {
    showMsg('pw-msg', d.error || d.detail || 'error', 'err');
  }
}

function showMsg(id, text, type) {
  const el = document.getElementById(id);
  el.textContent = text; el.className = 'msg ' + type; el.style.display = 'block';
  setTimeout(()=>el.style.display='none', 3000);
}

loadMe();
</script>
</body></html>"""


_REGISTER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-clips · request account</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:16px;padding:2rem;width:360px}
h1{font-size:1.4rem;font-weight:800;margin-bottom:.4rem;letter-spacing:-.5px}
h1 em{color:#f97316;font-style:normal}
.sub{font-size:.82rem;color:#555;margin-bottom:1.75rem;line-height:1.5}
label{display:block;font-size:.7rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.6px;margin-bottom:.35rem}
input{width:100%;padding:.7rem 1rem;background:#111;border:1px solid #2e2e2e;border-radius:8px;color:#eee;font-size:1rem;margin-bottom:1rem;outline:none;transition:border-color .15s}
input:focus{border-color:#f97316}
.btn{width:100%;padding:.8rem;background:#f97316;border:none;border-radius:8px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover:not([disabled]){background:#ea6c00}
.btn[disabled]{background:#444;cursor:not-allowed}
.msg{font-size:.82rem;margin-top:.75rem;text-align:center;display:none;line-height:1.5}
.msg.ok{color:#2ecc71}.msg.err{color:#e63946}
.login-link{text-align:center;margin-top:1.25rem;font-size:.8rem}
.login-link a{color:#555;text-decoration:none}
.login-link a:hover{color:#999}
.opt{color:#444;font-weight:400;text-transform:none;letter-spacing:0}
</style>
</head>
<body>
<div class="card">
  <h1>yt<em>-</em>clips</h1>
  <p class="sub">request an account — an admin will approve it before you can log in</p>
  <label>Username</label>
  <input id="un" type="text" placeholder="letters, numbers, hyphens" autocomplete="username">
  <label>Display Name <span class="opt">(optional)</span></label>
  <input id="dn" type="text" placeholder="your name, e.g. Spencer">
  <label>Password</label>
  <input id="pw" type="password" placeholder="password" autocomplete="new-password">
  <label>Confirm Password</label>
  <input id="cpw" type="password" placeholder="confirm password" autocomplete="new-password" onkeydown="if(event.key==='Enter')submit()">
  <button class="btn" id="submit-btn" onclick="submit()">Request Account</button>
  <div class="msg" id="msg"></div>
  <div class="login-link"><a href="/login">← back to login</a></div>
</div>
<script>
async function submit() {
  const un  = document.getElementById('un').value.trim().toLowerCase();
  const dn  = document.getElementById('dn').value.trim();
  const pw  = document.getElementById('pw').value;
  const cpw = document.getElementById('cpw').value;
  const btn = document.getElementById('submit-btn');
  if (!un || !pw) { showMsg('username and password required', 'err'); return; }
  if (pw !== cpw) { showMsg("passwords don't match", 'err'); return; }
  btn.disabled = true;
  const r = await fetch('/api/register', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username:un, display_name:dn, password:pw})
  });
  btn.disabled = false;
  const d = await r.json().catch(()=>({}));
  if (r.ok) {
    showMsg('request submitted — an admin will review it', 'ok');
    ['un','dn','pw','cpw'].forEach(id=>document.getElementById(id).value='');
    btn.style.display='none';
  } else {
    showMsg(d.detail || 'error submitting request', 'err');
  }
}
function showMsg(text, type) {
  const el=document.getElementById('msg');
  el.textContent=text; el.className='msg '+type; el.style.display='block';
  if(type==='err') setTimeout(()=>el.style.display='none', 4000);
}
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
    with _users_lock:
        display_name = users.get(username, {}).get("display_name", "")
    return {"username": username, "role": _get_role(username), "display_name": display_name}


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
    with _users_lock:
        display_names = {k: v.get("display_name") or k for k, v in users.items()}
    with _lock:
        return [
            {"job_id": k, "title": v.get("title"), "start_raw": v.get("start_raw"),
             "end_raw": v.get("end_raw"), "url": v.get("url"),
             "created_at": v["created_at"], "owner": v.get("owner") or ADMIN_USER,
             "owner_display": display_names.get(v.get("owner") or ADMIN_USER) or (v.get("owner") or ADMIN_USER)}
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


# ── Profile / account self-service ────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _get_user(request):
        return RedirectResponse("/login")
    return _SETTINGS_HTML


@app.put("/api/me/display_name")
async def update_display_name(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    data = await request.json()
    display_name = data.get("display_name", "").strip()
    with _users_lock:
        if username not in users:
            raise HTTPException(404)
        users[username]["display_name"] = display_name
    _save_users()
    return {"ok": True}


@app.put("/api/me/username")
async def update_username(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    data = await request.json()
    new_username = data.get("username", "").strip().lower()
    password = data.get("password", "")
    if not new_username:
        raise HTTPException(400, "username required")
    with _users_lock:
        user_data = dict(users.get(username, {}))
    if _hash_pw(username, password) != user_data.get("password_hash"):
        return JSONResponse({"ok": False, "error": "wrong password"}, status_code=401)
    if new_username == username:
        return JSONResponse({"ok": True, "username": username})
    with _users_lock:
        if new_username in users:
            raise HTTPException(409, "username already taken")
        new_hash = _hash_pw(new_username, password)
        users[new_username] = {**user_data, "password_hash": new_hash}
        del users[username]
    _save_users()
    with _lock:
        for v in jobs.values():
            if v.get("owner") == username:
                v["owner"] = new_username
    _save_jobs()
    new_token = _session_token(new_username, new_hash)
    r = JSONResponse({"ok": True, "username": new_username})
    r.set_cookie("auth", new_token, httponly=True, samesite="lax", max_age=86400 * 30)
    return r


@app.put("/api/me/password")
async def update_password(request: Request):
    username = _get_user(request)
    if not username:
        raise HTTPException(401)
    data = await request.json()
    current_pw = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    if not new_pw:
        raise HTTPException(400, "new_password required")
    with _users_lock:
        user_data = dict(users.get(username, {}))
    if _hash_pw(username, current_pw) != user_data.get("password_hash"):
        return JSONResponse({"ok": False, "error": "wrong current password"}, status_code=401)
    new_hash = _hash_pw(username, new_pw)
    with _users_lock:
        users[username]["password_hash"] = new_hash
    _save_users()
    new_token = _session_token(username, new_hash)
    r = JSONResponse({"ok": True})
    r.set_cookie("auth", new_token, httponly=True, samesite="lax", max_age=86400 * 30)
    return r


# ── Registration request flow ─────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return _REGISTER_HTML


@app.post("/api/register")
async def submit_registration(request: Request):
    data = await request.json()
    username = data.get("username", "").strip().lower()
    display_name = data.get("display_name", "").strip()
    password = data.get("password", "")
    if not username or not password:
        raise HTTPException(400, "username and password required")
    if not re.match(r'^[a-z0-9_-]{2,}$', username):
        raise HTTPException(400, "username must be 2+ chars: letters, numbers, hyphens, underscores")
    with _users_lock:
        if username in users:
            raise HTTPException(409, "username already taken")
    with _req_lock:
        if any(r["username"] == username for r in reg_requests):
            raise HTTPException(409, "a request for that username is already pending")
    req = {
        "id": str(uuid.uuid4()),
        "username": username,
        "display_name": display_name,
        "password_hash": _hash_pw(username, password),
        "requested_at": time.time(),
    }
    with _req_lock:
        reg_requests.append(req)
    _save_requests()
    return {"ok": True}


@app.get("/api/admin/requests")
async def admin_list_requests(request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    with _req_lock:
        return list(reg_requests)


@app.post("/api/admin/requests/{req_id}/approve")
async def admin_approve_request(req_id: str, request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    with _req_lock:
        req = next((r for r in reg_requests if r["id"] == req_id), None)
    if not req:
        raise HTTPException(404, "request not found")
    with _users_lock:
        if req["username"] in users:
            raise HTTPException(409, "username already taken")
        users[req["username"]] = {
            "password_hash": req["password_hash"],
            "role": "user",
            "display_name": req.get("display_name", ""),
        }
    _save_users()
    with _req_lock:
        reg_requests[:] = [r for r in reg_requests if r["id"] != req_id]
    _save_requests()
    return {"ok": True}


@app.delete("/api/admin/requests/{req_id}")
async def admin_reject_request(req_id: str, request: Request):
    if _get_role(_get_user(request) or "") != "admin":
        raise HTTPException(403)
    with _req_lock:
        before = len(reg_requests)
        reg_requests[:] = [r for r in reg_requests if r["id"] != req_id]
        if len(reg_requests) == before:
            raise HTTPException(404, "request not found")
    _save_requests()
    return {"ok": True}
