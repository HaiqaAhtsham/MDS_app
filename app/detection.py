"""Object detection on drone video and geo-location of the hits.

Pipeline for an uploaded video:
  1. sample a frame every `sample_every_s` seconds;
  2. find the object in the frame (YOLO weights if provided, otherwise
     multi-scale template matching against the reference pictures);
  3. turn each hit's pixel position into lat/lng using the drone's GPS fix,
     altitude and heading from the .srt file at that moment (the camera is
     assumed to point straight down);
  4. merge hits of the same object seen in consecutive frames;
  5. store one map marker per object, with a self-destruct expiry.
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import config, db, srt
from .geo import from_local, haversine

log = logging.getLogger("mds.detection")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class Hit:
    x: float  # box centre, pixels
    y: float
    w: float
    h: float
    score: float
    label: str = "ATM"  # marker type: ATM or APM


@dataclass
class Options:
    sample_every_s: float = 1.0
    hfov_deg: float = 73.7  # horizontal field of view of the camera
    threshold: float = 0.72  # template matching score (0-1)
    object_size_m: float | None = None  # real-world size, narrows the search
    min_hits: int = 1  # frames an object must appear in to become a marker
    merge_radius_m: float = 3.0
    max_width: int = 1920  # frames wider than this are downscaled
    altitude_m: float | None = None  # overrides the altitude in the .srt
    # Used when there is no .srt: a fixed camera position.
    lat: float | None = None
    lng: float | None = None
    label: str = "ATM"  # marker type: ATM or APM


# ---------------------------------------------------------------- detectors


def _nms(hits: list[Hit], iou: float = 0.3) -> list[Hit]:
    hits = sorted(hits, key=lambda h: h.score, reverse=True)
    kept: list[Hit] = []
    for h in hits:
        if all(_iou(h, k) < iou for k in kept):
            kept.append(h)
    return kept


def _iou(a: Hit, b: Hit) -> float:
    ax0, ay0, ax1, ay1 = a.x - a.w / 2, a.y - a.h / 2, a.x + a.w / 2, a.y + a.h / 2
    bx0, by0, bx1, by1 = b.x - b.w / 2, b.y - b.h / 2, b.x + b.w / 2, b.y + b.h / 2
    iw, ih = max(0, min(ax1, bx1) - max(ax0, bx0)), max(0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    return inter / (a.w * a.h + b.w * b.h - inter + 1e-9)


class TemplateDetector:
    """Few-shot detector: normalised cross-correlation against each
    reference picture at several sizes and 4 rotations. Works best when the
    reference pictures are tight, top-down crops of the object."""

    SIZES_PX = (14, 20, 28, 40, 56, 80, 112)
    MAX_PEAKS = 200

    def __init__(self, reference_dir: Path):
        self.templates: list[np.ndarray] = []
        for p in sorted(reference_dir.iterdir()) if reference_dir.exists() else []:
            if p.suffix.lower() in IMAGE_EXT:
                img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if img is not None and min(img.shape) >= 8:
                    self.templates.append(img)
        if not self.templates:
            raise RuntimeError(f"No reference pictures found in {reference_dir}")

    def detect(self, frame: np.ndarray, threshold: float, expected_px: float | None, label: str) -> list[Hit]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if expected_px:
            sizes = [s for s in (expected_px * 0.8, expected_px, expected_px * 1.25) if s >= 8]
        else:
            sizes = list(self.SIZES_PX)
        hits: list[Hit] = []
        for tpl in self.templates:
            for size in sizes:
                scale = size / max(tpl.shape)
                t = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1] or min(t.shape) < 6:
                    continue
                for rot in range(4):
                    tr = np.ascontiguousarray(np.rot90(t, rot))
                    res = cv2.matchTemplate(gray, tr, cv2.TM_CCOEFF_NORMED)
                    th, tw = tr.shape
                    # Keep local maxima only, otherwise every pixel around a
                    # match counts as a separate hit.
                    k = max(3, (min(th, tw) // 2) | 1)
                    peaks = (res >= threshold) & (res >= cv2.dilate(res, np.ones((k, k), np.uint8)))
                    ys, xs = np.nonzero(peaks)
                    if len(ys) > self.MAX_PEAKS:
                        top = np.argsort(res[ys, xs])[-self.MAX_PEAKS:]
                        ys, xs = ys[top], xs[top]
                    for y, x in zip(ys, xs):
                        hits.append(Hit(x + tw / 2, y + th / 2, tw, th, float(res[y, x]), label))
        return _nms(hits)


class YoloDetector:
    def __init__(self, weights: Path):
        from ultralytics import YOLO  # optional dependency

        self.model = YOLO(str(weights))

    def detect(self, frame: np.ndarray, threshold: float, expected_px: float | None, label: str) -> list[Hit]:
        hits = []
        for r in self.model.predict(frame, conf=min(threshold, 0.5), verbose=False):
            for box, conf, cls in zip(r.boxes.xywh.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                name = str(r.names.get(int(cls), "")).upper()  # a model trained with ATM/APM classes
                hits.append(Hit(box[0], box[1], box[2], box[3], float(conf), name if name in ("ATM", "APM") else label))
        return hits


def load_detector():
    if config.YOLO_WEIGHTS.exists():
        try:
            return YoloDetector(config.YOLO_WEIGHTS)
        except Exception as e:  # ultralytics missing or bad weights
            log.warning("Could not load YOLO weights (%s); falling back to template matching", e)
    return TemplateDetector(config.REFERENCE_DIR)


# ------------------------------------------------------------ geo-location


def metres_per_pixel(alt_m: float, hfov_deg: float, width_px: int) -> float:
    ground_width = 2 * alt_m * math.tan(math.radians(hfov_deg) / 2)
    return ground_width / width_px


def pixel_to_latlng(x: float, y: float, width: int, height: int, lat: float, lng: float,
                    alt_m: float, hfov_deg: float, yaw_deg: float | None) -> tuple[float, float]:
    """Nadir camera model: the frame centre is directly below the drone and
    the top of the frame faces the heading `yaw_deg` (clockwise from north)."""
    mpp = metres_per_pixel(alt_m, hfov_deg, width)
    right = (x - width / 2) * mpp
    up = (height / 2 - y) * mpp
    yaw = math.radians(yaw_deg or 0.0)
    east = up * math.sin(yaw) + right * math.cos(yaw)
    north = up * math.cos(yaw) - right * math.sin(yaw)
    return from_local(lat, lng, east, north)


# --------------------------------------------------------------- clustering


@dataclass
class Cluster:
    lat: float
    lng: float
    score: float
    hits: int = 1
    crop: np.ndarray | None = field(default=None, repr=False)
    label: str = "object"


def cluster(points: list[tuple[float, float, float, np.ndarray | None, str]], radius_m: float) -> list[Cluster]:
    """Greedy merge of (lat, lng, score, crop, label) points closer than radius_m."""
    out: list[Cluster] = []
    for lat, lng, score, crop, label in sorted(points, key=lambda p: -p[2]):
        for c in out:
            if haversine(lat, lng, c.lat, c.lng) <= radius_m:
                n = c.hits
                c.lat = (c.lat * n + lat) / (n + 1)
                c.lng = (c.lng * n + lng) / (n + 1)
                c.hits += 1
                break
        else:
            out.append(Cluster(lat, lng, score, 1, crop, label))
    return out


# --------------------------------------------------------------------- jobs


def create_job(filename: str, user_id: int | None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute(
            "INSERT INTO jobs (id, filename, status, created_at, created_by) VALUES (?, ?, 'queued', ?, ?)",
            (job_id, filename, time.time(), user_id),
        )
    return job_id


def _update_job(job_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def run_job(job_id: str, video: Path, srt_path: Path | None, opts: Options, user_id: int | None) -> None:
    try:
        _update_job(job_id, status="running", message="Loading detector")
        detector = load_detector()
        fixes = srt.parse(srt_path.read_text(errors="ignore")) if srt_path else []
        if not fixes and (opts.lat is None or opts.lng is None):
            raise ValueError("No GPS in the .srt file; give the camera position manually")
        if not fixes and opts.altitude_m is None:
            raise ValueError("Give the flight altitude when there is no .srt file")

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise ValueError("Could not open the video file")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        step = max(1, int(round(fps * opts.sample_every_s)))
        points = []
        n_hits = 0
        idx = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                n_hits += _process_frame(frame, idx / fps, fixes, detector, opts, points)
                if frames:
                    _update_job(job_id, progress=min(0.99, idx / frames), detections=n_hits,
                                message=f"Frame {idx}/{frames}")
            idx += 1
        cap.release()

        clusters = [c for c in cluster(points, opts.merge_radius_m) if c.hits >= opts.min_hits]
        saved = save_markers(clusters, source=video.name, user_id=user_id)
        _update_job(job_id, status="done", progress=1.0, detections=n_hits, markers=saved,
                    message=f"{saved} object(s) marked from {n_hits} detection(s)")
    except Exception as e:
        log.exception("Detection job %s failed", job_id)
        _update_job(job_id, status="error", message=str(e))


def _process_frame(frame, t, fixes, detector, opts: Options, points: list) -> int:
    if fixes:
        fix = srt.fix_at(fixes, t)
        lat, lng, yaw = fix.lat, fix.lng, fix.yaw
        alt = opts.altitude_m if opts.altitude_m is not None else fix.alt
    else:
        lat, lng, yaw, alt = opts.lat, opts.lng, None, opts.altitude_m
    if not alt or alt <= 0:
        return 0  # on the ground / no altitude: cannot geo-locate

    h, w = frame.shape[:2]
    if w > opts.max_width:
        s = opts.max_width / w
        frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]

    expected_px = None
    if opts.object_size_m:
        expected_px = opts.object_size_m / metres_per_pixel(alt, opts.hfov_deg, w)
    hits = detector.detect(frame, opts.threshold, expected_px, opts.label)
    for hit in hits:
        hlat, hlng = pixel_to_latlng(hit.x, hit.y, w, h, lat, lng, alt, opts.hfov_deg, yaw)
        pad = max(hit.w, hit.h)
        x0, y0 = int(max(0, hit.x - pad)), int(max(0, hit.y - pad))
        crop = frame[y0:int(hit.y + pad), x0:int(hit.x + pad)].copy()
        points.append((hlat, hlng, hit.score, crop, hit.label))
    return len(hits)


def save_markers(clusters: list[Cluster], source: str, user_id: int | None) -> int:
    sd_hours = float(db.get_setting("sd_hours", str(config.DEFAULT_SD_HOURS)))
    now = time.time()
    with db.tx() as c:
        for cl in clusters:
            crop_name = None
            if cl.crop is not None and cl.crop.size:
                crop_name = f"{uuid.uuid4().hex[:16]}.jpg"
                cv2.imwrite(str(config.CROP_DIR / crop_name), cl.crop)
            c.execute(
                """INSERT INTO markers (lat, lng, label, score, hits, source, crop, created_at, expires_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cl.lat, cl.lng, cl.label, round(cl.score, 3), cl.hits, source, crop_name,
                 now, now + sd_hours * 3600, user_id),
            )
    return len(clusters)
