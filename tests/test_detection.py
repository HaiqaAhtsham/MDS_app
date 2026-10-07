"""End-to-end: synthetic nadir drone video + .srt -> markers at the right place."""

import time

import cv2
import numpy as np

from app import detection
from app.geo import haversine

DRONE = (33.60000, 73.00000)
ALT = 40.0
HFOV = 73.7
W, H = 960, 540


def _object(size=40):
    """A distinctive round object with a cross, seen from above."""
    img = np.full((size, size, 3), 70, np.uint8)
    cv2.circle(img, (size // 2, size // 2), size // 2 - 2, (40, 180, 230), -1)
    cv2.line(img, (6, size // 2), (size - 6, size // 2), (20, 20, 20), 3)
    cv2.line(img, (size // 2, 6), (size // 2, size - 6), (20, 20, 20), 3)
    return img


def _ground(rng):
    base = rng.integers(60, 110, (H // 8, W // 8, 3), dtype=np.uint8)
    base[..., 1] += 30  # greenish fields
    return cv2.GaussianBlur(cv2.resize(base, (W, H), interpolation=cv2.INTER_NEAREST), (0, 0), 3)


def _write_inputs(tmp_path, positions_px):
    rng = np.random.default_rng(0)
    ground = _ground(rng)
    obj = _object()
    video = tmp_path / "flight.mp4"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (W, H))
    for _ in range(30):  # 3 s at 10 fps, drone hovering
        frame = ground.copy()
        for x, y in positions_px:
            frame[y - 20:y + 20, x - 20:x + 20] = obj
        out.write(frame)
    out.release()
    srt_text = "".join(
        f"{i + 1}\n00:00:0{i},000 --> 00:00:0{i},999\n"
        f"[latitude: {DRONE[0]}] [longitude: {DRONE[1]}] [rel_alt: {ALT} abs_alt: 500] [gb_yaw: 0.0 gb_pitch: -90.0]\n\n"
        for i in range(3)
    )
    (tmp_path / "flight.srt").write_text(srt_text)
    return video, tmp_path / "flight.srt", obj


def test_pixel_to_latlng_north_up():
    mpp = detection.metres_per_pixel(ALT, HFOV, W)
    # 100 px right of centre is ~100*mpp metres east.
    lat, lng = detection.pixel_to_latlng(W / 2 + 100, H / 2, W, H, *DRONE, ALT, HFOV, 0)
    assert abs(haversine(*DRONE, lat, lng) - 100 * mpp) < 0.05
    assert lng > DRONE[1] and abs(lat - DRONE[0]) < 1e-9
    # With heading 90 (east), the top of the frame points east.
    lat, lng = detection.pixel_to_latlng(W / 2, H / 2 - 100, W, H, *DRONE, ALT, HFOV, 90)
    assert lng > DRONE[1] and abs(lat - DRONE[0]) < 1e-7


def test_video_to_markers(client, app_env):
    from app import config

    positions = [(300, 200), (700, 380)]
    video, srt_path, obj = _write_inputs(app_env, positions)
    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(config.REFERENCE_DIR / "object.png"), obj)

    with video.open("rb") as v, srt_path.open("rb") as s:
        r = client.post(
            "/api/detections",
            files={"video": ("flight.mp4", v, "video/mp4"), "telemetry": ("flight.srt", s, "text/plain")},
            data={"sample_every_s": "1", "hfov_deg": str(HFOV), "min_hits": "2", "label": "APM"},
        )
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert job["status"] == "done", job
    assert job["markers"] == 2

    markers = client.get("/api/markers").json()["markers"]
    assert len(markers) == 2
    for x, y in positions:
        want = detection.pixel_to_latlng(x, y, W, H, *DRONE, ALT, HFOV, 0)
        nearest = min(haversine(*want, m["lat"], m["lng"]) for m in markers)
        assert nearest < 0.5, f"marker {nearest:.2f} m from truth"
    assert all(m["label"] == "APM" and m["crop"] for m in markers)
    # Default self-destruct time (48 h) applied.
    assert all(abs(m["expires_at"] - m["created_at"] - 48 * 3600) < 1 for m in markers)


def test_self_destruct_expiry(client):
    client.post("/api/markers", json={"lat": 33.6, "lng": 73.0, "kind": "ATM"})
    client.post("/api/markers", json={"lat": 33.601, "lng": 73.0, "kind": "APM"})
    assert len(client.get("/api/markers").json()["markers"]) == 2

    # Out of the MLCU range 1-272 h.
    assert client.put("/api/settings/sd", json={"hours": 300}).status_code == 422

    # Re-time existing markers; then pretend they were marked 13 h ago.
    client.put("/api/settings/sd", json={"hours": 12, "apply_to_existing": True})
    markers = client.get("/api/markers").json()["markers"]
    assert all(abs(m["expires_at"] - m["created_at"] - 12 * 3600) < 1 for m in markers)

    from app import db, main

    with db.tx() as c:
        c.execute("UPDATE markers SET created_at = created_at - 13 * 3600, expires_at = expires_at - 13 * 3600")
    assert client.get("/api/markers").json()["markers"] == []  # hidden immediately
    assert main.purge_expired() == 2  # and purged by the cleanup loop


def test_reference_crop(client, app_env):
    from app import config

    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    big = np.full((100, 120, 3), 90, np.uint8)
    big[40:60, 50:70] = _object(20)
    cv2.imwrite(str(config.REFERENCE_DIR / "shot.jpg"), big)
    assert client.post("/api/reference/shot.jpg/crop", json={"x": 100, "y": 0, "w": 40, "h": 20}).status_code == 400
    r = client.post("/api/reference/shot.jpg/crop", json={"x": 50, "y": 40, "w": 20, "h": 20}).json()
    assert r["saved"] == "shot.png" and not (config.REFERENCE_DIR / "shot.jpg").exists()
    assert cv2.imread(str(config.REFERENCE_DIR / "shot.png")).shape[:2] == (20, 20)


def test_no_detections_reports_best_match_and_reasons(client, app_env):
    """A reference that isn't in the video: the job explains what it saw."""
    from app import config

    video, srt_path, obj = _write_inputs(app_env, [(300, 200)])
    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    other = np.zeros((40, 40, 3), np.uint8)
    cv2.rectangle(other, (5, 5), (35, 35), (255, 255, 255), 3)  # a square, not our round object
    cv2.imwrite(str(config.REFERENCE_DIR / "square.png"), other)
    with video.open("rb") as v, srt_path.open("rb") as s:
        job_id = client.post("/api/detections", files={"video": ("f.mp4", v, "video/mp4"), "telemetry": ("f.srt", s, "text/plain")},
                             data={"hfov_deg": str(HFOV), "threshold": "0.9"}).json()["job_id"]
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert job["status"] == "done" and job["markers"] == 0
    assert job["frames_checked"] == 3 and "Best match" in job["message"] and job["best_crop"]
    assert 0 < job["best_score"] < 0.9


def test_srt_without_altitude_is_explained(client, app_env):
    from app import config

    video, srt_path, obj = _write_inputs(app_env, [(300, 200)])
    srt_path.write_text(srt_path.read_text().replace(f"[rel_alt: {ALT} abs_alt: 500] ", ""))
    config.REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(config.REFERENCE_DIR / "object.png"), obj)
    with video.open("rb") as v, srt_path.open("rb") as s:
        job_id = client.post("/api/detections", files={"video": ("f.mp4", v, "video/mp4"), "telemetry": ("f.srt", s, "text/plain")},
                             data={"hfov_deg": str(HFOV)}).json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert job["status"] == "error" and "flight height" in job["message"]
