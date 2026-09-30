"""MDS Dashboard web app (FastAPI)."""

import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, config, db, detection, rag, tracking

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mds")
STATIC = config.ROOT / "static"
_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detect")
_stop = threading.Event()


def _purge_expired_loop() -> None:
    while not _stop.wait(60):
        purge_expired()


def purge_expired() -> int:
    with db.tx() as c:
        return c.execute("DELETE FROM markers WHERE expires_at <= ?", (time.time(),)).rowcount


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    auth.bootstrap_admin()
    with db.tx() as c:  # jobs interrupted by a restart
        c.execute("UPDATE jobs SET status = 'error', message = 'Interrupted by restart' WHERE status IN ('queued', 'running')")
    threading.Thread(target=rag.build_index, daemon=True).start()
    threading.Thread(target=_purge_expired_loop, daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="MDS Dashboard", lifespan=lifespan)
User = Depends(auth.current_user)
Admin = Depends(auth.require_admin)


# ------------------------------------------------------------------ auth


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    token = auth.login(body.username.strip(), body.password)
    if not token:
        time.sleep(0.5)  # slow down guessing
        raise HTTPException(401, "Wrong username or password")
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                        max_age=config.SESSION_HOURS * 3600)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    if token := request.cookies.get(auth.COOKIE):
        auth.logout(token)
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=User):
    return user


class PasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/me/password")
def change_password(body: PasswordIn, user=User):
    auth.validate_password(body.new)
    with db.tx() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not auth.verify_password(body.current, row["password_hash"]):
            raise HTTPException(400, "Current password is wrong")
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(body.new), user["id"]))
    return {"ok": True}


# ----------------------------------------------------------------- users


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str


class UserPatch(BaseModel):
    password: str | None = None
    active: bool | None = None


@app.get("/api/users")
def list_users(admin=Admin):
    with db.tx() as c:
        rows = c.execute("SELECT id, username, role, active, created_at FROM users ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/users")
def create_user(body: UserIn, admin=Admin):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", body.username):
        raise HTTPException(400, "Username may only contain letters, digits, _ . -")
    auth.validate_password(body.password)
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(409, "Username already exists")
        # There is a single admin; everyone added here is an operator.
        cur = c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'operator', ?)",
            (body.username, auth.hash_password(body.password), time.time()),
        )
    return {"id": cur.lastrowid}


def _operator(c, user_id: int):
    row = c.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such user")
    if row["role"] == "admin":
        raise HTTPException(400, "The admin account can't be changed here")


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin=Admin):
    with db.tx() as c:
        _operator(c, user_id)
        if body.password is not None:
            auth.validate_password(body.password)
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(body.password), user_id))
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if body.active is not None:
            c.execute("UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id))
            if not body.active:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin=Admin):
    with db.tx() as c:
        _operator(c, user_id)
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


# -------------------------------------------------------------- tracking

LatLng = tuple[float, float]


def _check(p: LatLng) -> tuple[float, float]:
    if not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180):
        raise HTTPException(400, "Invalid coordinates")
    return p


class RouteIn(BaseModel):
    start: LatLng
    end: LatLng


class PositionIn(BaseModel):
    lat: float
    lng: float
    accuracy: float | None = None
    heading: float | None = None
    speed: float | None = None


@app.get("/api/tracking")
def tracking_state(user=User):
    return tracking.state()


@app.post("/api/tracking/route")
def set_route(body: RouteIn, user=User):
    tracking.set_route(_check(body.start), _check(body.end))
    return tracking.state()


@app.post("/api/tracking/start")
def start_mission(user=User):
    try:
        return tracking.start_mission()
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/tracking/reset")
def reset_mission(user=User):
    tracking.reset_mission()
    return tracking.state()


@app.post("/api/tracking/position")
def post_position(body: PositionIn, user=User):
    _check((body.lat, body.lng))
    return tracking.record_position(body.lat, body.lng, body.accuracy, body.heading, body.speed, user["id"])


# --------------------------------------------------------------- markers


class MarkerIn(BaseModel):
    lat: float
    lng: float
    label: str = "object"


def _markers():
    with db.tx() as c:
        rows = c.execute("SELECT * FROM markers WHERE expires_at > ? ORDER BY id", (time.time(),)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/markers")
def list_markers(user=User):
    return {"now": time.time(), "markers": _markers()}


@app.post("/api/markers")
def add_marker(body: MarkerIn, user=User):
    _check((body.lat, body.lng))
    sd = float(db.get_setting("sd_hours"))
    now = time.time()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO markers (lat, lng, label, source, created_at, expires_at, created_by) VALUES (?, ?, ?, 'manual', ?, ?, ?)",
            (body.lat, body.lng, body.label[:40] or "object", now, now + sd * 3600, user["id"]),
        )
    return {"id": cur.lastrowid}


@app.delete("/api/markers/{marker_id}")
def delete_marker(marker_id: int, user=User):
    with db.tx() as c:
        c.execute("DELETE FROM markers WHERE id = ?", (marker_id,))
    return {"ok": True}


@app.delete("/api/markers")
def clear_markers(user=User):
    with db.tx() as c:
        c.execute("DELETE FROM markers")
    return {"ok": True}


@app.get("/api/crops/{name}")
def crop(name: str, user=User):
    if not re.fullmatch(r"[0-9a-f]{16}\.jpg", name):
        raise HTTPException(404)
    path = config.CROP_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# -------------------------------------------------------------- settings


class SdIn(BaseModel):
    hours: float = Field(ge=config.SD_MIN_HOURS, le=config.SD_MAX_HOURS)
    apply_to_existing: bool = False


class AmmoIn(BaseModel):
    fired: int = Field(ge=0)
    total: int | None = Field(default=None, ge=1)


def _settings():
    return {
        "sd_hours": float(db.get_setting("sd_hours")),
        "sd_min": config.SD_MIN_HOURS,
        "sd_max": config.SD_MAX_HOURS,
        "total_canisters": int(db.get_setting("total_canisters")),
        "fired_canisters": int(db.get_setting("fired_canisters")),
        "rag_mode": config.RAG_MODE,
    }


@app.get("/api/settings")
def get_settings(user=User):
    return _settings()


@app.put("/api/settings/sd")
def set_sd(body: SdIn, user=User):
    db.set_setting("sd_hours", body.hours)
    if body.apply_to_existing:
        # Self-destruct time counts from when the object was marked.
        with db.tx() as c:
            c.execute("UPDATE markers SET expires_at = created_at + ?", (body.hours * 3600,))
        purge_expired()
    return _settings()


@app.put("/api/settings/ammo")
def set_ammo(body: AmmoIn, user=User):
    total = body.total or int(db.get_setting("total_canisters"))
    if body.fired > total:
        raise HTTPException(400, "Fired can't exceed total")
    db.set_setting("total_canisters", total)
    db.set_setting("fired_canisters", body.fired)
    return _settings()


# ------------------------------------------------------------- detection

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def _save_upload(upload: UploadFile, dest_dir: Path, allowed: set[str]) -> Path:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}; expected {', '.join(sorted(allowed))}")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(upload.filename).stem)[:60]
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f, length=1 << 20)
    return dest


@app.post("/api/detections")
def upload_video(
    video: UploadFile = File(...),
    telemetry: UploadFile | None = File(None),
    sample_every_s: float = Form(1.0),
    hfov_deg: float = Form(73.7),
    threshold: float = Form(0.72),
    object_size_m: float | None = Form(None),
    min_hits: int = Form(1),
    altitude_m: float | None = Form(None),
    lat: float | None = Form(None),
    lng: float | None = Form(None),
    label: str = Form("object"),
    user=User,
):
    if not (0.05 <= sample_every_s <= 30 and 10 <= hfov_deg <= 170 and 0.3 <= threshold <= 0.99):
        raise HTTPException(400, "Detection options out of range")
    video_path = _save_upload(video, config.UPLOAD_DIR, VIDEO_EXT)
    srt_path = _save_upload(telemetry, config.UPLOAD_DIR, {".srt"}) if telemetry and telemetry.filename else None
    opts = detection.Options(
        sample_every_s=sample_every_s, hfov_deg=hfov_deg, threshold=threshold,
        object_size_m=object_size_m or None, min_hits=max(1, min_hits), altitude_m=altitude_m,
        lat=lat, lng=lng, label=(label or "object")[:40],
    )
    job_id = detection.create_job(video.filename, user["id"])
    _jobs.submit(detection.run_job, job_id, video_path, srt_path, opts, user["id"])
    return {"job_id": job_id}


@app.get("/api/jobs")
def list_jobs(user=User):
    with db.tx() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user=User):
    with db.tx() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.get("/api/reference")
def list_reference(user=User):
    if not config.REFERENCE_DIR.exists():
        return []
    return sorted(p.name for p in config.REFERENCE_DIR.iterdir() if p.suffix.lower() in detection.IMAGE_EXT)


@app.post("/api/reference")
def upload_reference(files: list[UploadFile] = File(...), admin=Admin):
    return {"saved": [_save_upload(f, config.REFERENCE_DIR, detection.IMAGE_EXT).name for f in files]}


def _reference_path(name: str) -> Path:
    path = (config.REFERENCE_DIR / name).resolve()
    if path.parent != config.REFERENCE_DIR.resolve() or not path.exists():
        raise HTTPException(404)
    return path


@app.get("/api/reference/{name}")
def reference_image(name: str, user=User):
    return FileResponse(_reference_path(name))


@app.delete("/api/reference/{name}")
def delete_reference(name: str, admin=Admin):
    _reference_path(name).unlink()
    return {"ok": True}


# ------------------------------------------------------------------ chat


class ChatIn(BaseModel):
    question: str = Field(max_length=2000)
    history: list[dict] = []


@app.post("/api/chat")
def chat(body: ChatIn, user=User):
    return rag.answer(body.question, body.history)


@app.get("/api/docs")
def list_docs(user=User):
    return rag.documents()


@app.post("/api/docs")
def upload_doc(file: UploadFile = File(...), admin=Admin):
    path = _save_upload(file, config.DOC_UPLOAD_DIR, {".pdf"})
    rag.build_index()
    return {"saved": path.name}


@app.delete("/api/docs/{name}")
def delete_doc(name: str, admin=Admin):
    path = (config.DOC_UPLOAD_DIR / name).resolve()
    if path.parent != config.DOC_UPLOAD_DIR.resolve() or not path.exists():
        raise HTTPException(404, "Only uploaded documents can be removed")
    path.unlink()
    rag.build_index()
    return {"ok": True}


# ---------------------------------------------------------------- static


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/logo.svg")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
