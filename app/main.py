"""MDS Dashboard web app (FastAPI)."""

import logging
import re
import shutil
import sqlite3
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

from . import auth, config, db, density, detection, mgrs, rag, routing, tracking

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


class PointIn(BaseModel):
    which: str = Field(pattern="^(start|end)$")
    lat: float
    lng: float


class ParamsIn(BaseModel):
    mine_type: str = Field(pattern="^(at|mixed)$")
    atm: float
    apm: float | None = None
    time_gap: int


@app.post("/api/tracking/point")
def set_point(body: PointIn, user=User):
    _check((body.lat, body.lng))
    tracking.set_point(body.which, body.lat, body.lng)
    return tracking.state()


@app.put("/api/tracking/params")
def set_params(body: ParamsIn, user=User):
    try:
        tracking.set_params(body.mine_type, body.atm, body.apm, body.time_gap)
    except density.SelectionError as e:
        raise HTTPException(400, str(e))
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


# ------------------------------------------------------ density / grid / route


@app.get("/api/density")
def density_table(user=User):
    return density.table()


@app.post("/api/density/calc")
def density_calc(body: ParamsIn, user=User):
    try:
        return density.calculate(body.mine_type, body.time_gap, body.atm, body.apm)
    except density.SelectionError as e:
        raise HTTPException(400, str(e))


class GridIn(BaseModel):
    text: str = Field(max_length=40)
    gzd: str | None = None
    square: str | None = None


@app.post("/api/grid/parse")
def grid_parse(body: GridIn, user=User):
    try:
        return mgrs.parse(body.text, body.gzd or db.get_setting("grid_gzd"), body.square or db.get_setting("grid_square"))
    except mgrs.GridError as e:
        raise HTTPException(400, str(e))


@app.get("/api/route")
def get_route(from_lat: float, from_lng: float, to_lat: float, to_lng: float, user=User):
    _check((from_lat, from_lng))
    _check((to_lat, to_lng))
    return routing.route((from_lat, from_lng), (to_lat, to_lng))


def _tile_source() -> str | None:
    if config.MBTILES.exists():
        return "mbtiles"
    if config.TILE_DIR.exists() and any(p.is_dir() and p.name.isdigit() for p in config.TILE_DIR.iterdir()):
        return "dir"
    return None


@app.get("/api/config")
def client_config(user=User):
    local = _tile_source()
    max_zoom = 19
    if local == "mbtiles":
        conn = sqlite3.connect(f"file:{config.MBTILES}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM metadata WHERE name = 'maxzoom'").fetchone()
            max_zoom = int(row[0]) if row else max_zoom
        except (sqlite3.Error, ValueError):
            pass
        finally:
            conn.close()
    elif local == "dir":
        max_zoom = max(int(p.name) for p in config.TILE_DIR.iterdir() if p.is_dir() and p.name.isdigit())
    return {
        "tile_max_zoom": max_zoom,
        "tile_url": "/tiles/{z}/{x}/{y}" if local else (config.ONLINE_TILE_URL if config.ONLINE_TILES else None),
        "tiles": local or ("online" if config.ONLINE_TILES else "none"),
        "road_routing": bool(config.OSRM_URL),
        "off_route_m": config.OFF_ROUTE_M,
        "arrival_radius_m": config.ARRIVAL_RADIUS_M,
    }


@app.get("/tiles/{z}/{x}/{y}")
def tile(z: int, x: int, y: int, user=User):
    source = _tile_source()
    if source == "mbtiles":
        conn = sqlite3.connect(f"file:{config.MBTILES}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                (z, x, (1 << z) - 1 - y),  # MBTiles rows are TMS (flipped)
            ).fetchone()
            fmt = conn.execute("SELECT value FROM metadata WHERE name = 'format'").fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(404)
        kind = "jpeg" if fmt and fmt[0] in ("jpg", "jpeg") else "png"
        return Response(row[0], media_type=f"image/{kind}", headers={"Cache-Control": "max-age=86400"})
    if source == "dir":
        for ext in ("jpg", "jpeg", "png", "webp"):
            path = config.TILE_DIR / str(z) / str(x) / f"{y}.{ext}"
            if path.exists():
                return FileResponse(path, headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(404)


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
        "speed_tol_pct": float(db.get_setting("speed_tol_pct", "10")),
        "grid_gzd": db.get_setting("grid_gzd", ""),
        "grid_square": db.get_setting("grid_square", ""),
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


class GridDefaultsIn(BaseModel):
    gzd: str
    square: str


class ToleranceIn(BaseModel):
    pct: float = Field(gt=0, le=100)


@app.put("/api/settings/grid")
def set_grid_defaults(body: GridDefaultsIn, user=User):
    try:
        mgrs.from_mgrs(body.gzd, body.square, "00")  # validates zone, band and square
    except mgrs.GridError as e:
        raise HTTPException(400, str(e))
    db.set_setting("grid_gzd", body.gzd.strip().upper().replace(" ", ""))
    db.set_setting("grid_square", body.square.strip().upper().replace(" ", ""))
    return _settings()


@app.put("/api/settings/speed-tolerance")
def set_speed_tolerance(body: ToleranceIn, admin=Admin):
    db.set_setting("speed_tol_pct", body.pct)
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
