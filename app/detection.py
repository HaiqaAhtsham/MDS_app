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
import os
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
    threshold: float = 0.65  # template matching score (0-1)
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
    reference picture at many sizes and 4 rotations. Works best when the
    reference pictures are tight, top-down crops of the object.

    Large sizes are matched on a downscaled copy of the frame (the template
    is never bigger than WORK_PX there), which keeps the wide size search fast.
    """

    MIN_PX, SIZE_STEP, WORK_PX = 10, 1.15, 64
    COARSE_SLACK = 0.15  # coarse pass keeps candidates this far below the threshold
    BACKGROUND_PCT = 99.0  # "typical good match" level of a frame, per size and rotation
    MIN_CONTRAST = float(os.environ.get("MDS_MIN_CONTRAST", "0.35"))  # tuned on test flights
    MAX_PEAKS = 200
    MAX_REFINE = 60

    def __init__(self, reference_dir: Path):
        self.templates: list[tuple[str, np.ndarray]] = []
        for p in sorted(reference_dir.iterdir()) if reference_dir.exists() else []:
            if p.suffix.lower() in IMAGE_EXT:
                img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if img is not None and min(img.shape) >= 8:
                    self.templates.append((p.name, img))
        if not self.templates:
            raise RuntimeError(f"No reference pictures found in {reference_dir}")
        self.best: dict | None = None  # best match seen, even below the threshold

    def sizes(self, tpl: np.ndarray, frame_h: int, expected_px: float | None, frame_scale: float) -> list[float]:
        """Template sizes (longest side, px in the processed frame) to try."""
        if expected_px:
            # Object size given: a band around it (height/FOV are rarely exact).
            return [expected_px * f for f in (0.6, 0.7, 0.8, 0.9, 1.0, 1.12, 1.25, 1.4, 1.6) if expected_px * f >= 8]
        top = max(self.MIN_PX * 2, frame_h / 3)
        out, x = [], float(self.MIN_PX)
        while x <= top:
            out.append(x)
            x *= self.SIZE_STEP
        # A screenshot cut from the same video: its own size, scaled like the frame.
        native = max(tpl.shape) * frame_scale
        if self.MIN_PX <= native <= top:
            out += [native * 0.9, native, native * 1.1]
        return sorted(out)

    def _template(self, tpl: np.ndarray, size: float, rot: int) -> np.ndarray | None:
        scale = size / max(tpl.shape)
        t = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if min(t.shape) < 6:
            return None
        return np.ascontiguousarray(np.rot90(t, rot))

    def detect(self, frame: np.ndarray, threshold: float, expected_px: float | None, label: str,
               frame_scale: float = 1.0) -> list[Hit]:
        """Two passes. Coarse: every size and rotation, keep anything within
        COARSE_SLACK of the threshold. Fine: around each candidate, re-match
        at every pixel size nearby (small objects need the size to the pixel)."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        pyramid: dict[float, np.ndarray] = {1.0: gray}
        candidates: list[tuple[Hit, np.ndarray, str]] = []
        coarse_thr = max(0.2, threshold - self.COARSE_SLACK)
        for name, tpl in self.templates:
            for size in self.sizes(tpl, gray.shape[0], expected_px, frame_scale):
                work = round(min(1.0, self.WORK_PX / size), 3)  # match large sizes on a smaller frame
                g = pyramid.get(work)
                if g is None:
                    g = pyramid[work] = cv2.resize(gray, None, fx=work, fy=work, interpolation=cv2.INTER_AREA)
                for rot in range(4):
                    tr = self._template(tpl, size * work, rot)
                    if tr is None or tr.shape[0] >= g.shape[0] or tr.shape[1] >= g.shape[1]:
                        continue
                    res = cv2.matchTemplate(g, tr, cv2.TM_CCOEFF_NORMED)
                    th, tw = tr.shape
                    _, mx, _, loc = cv2.minMaxLoc(res)
                    self._note_best(mx, size, name, (loc[0] + tw / 2) / work, (loc[1] + th / 2) / work, tw / work, th / work)
                    if mx < coarse_thr:
                        continue
                    # A real object stands out from what this template matches
                    # elsewhere in the frame; ground texture doesn't.
                    floor = float(np.percentile(res[::3, ::3], self.BACKGROUND_PCT))
                    need = max(coarse_thr, floor + self.MIN_CONTRAST)
                    if mx < need:
                        continue
                    k = max(3, (min(th, tw) // 2) | 1)
                    peaks = (res >= need) & (res >= cv2.dilate(res, np.ones((k, k), np.uint8)))
                    ys, xs = np.nonzero(peaks)
                    if len(ys) > self.MAX_PEAKS:
                        top = np.argsort(res[ys, xs])[-self.MAX_PEAKS:]
                        ys, xs = ys[top], xs[top]
                    for y, x in zip(ys, xs):
                        hit = Hit((x + tw / 2) / work, (y + th / 2) / work, tw / work, th / work, float(res[y, x]), label)
                        candidates.append((hit, tpl, name))
        # Fine pass on the strongest, non-overlapping candidates.
        candidates.sort(key=lambda c: -c[0].score)
        kept: list[tuple[Hit, np.ndarray, str]] = []
        for c in candidates:
            if all(_iou(c[0], k[0]) < 0.3 for k in kept):
                kept.append(c)
            if len(kept) >= self.MAX_REFINE:
                break
        hits = []
        for cand, tpl, name in kept:
            h = self._refine(gray, cand, tpl, name)
            if h.score >= threshold:
                hits.append(h)
        return _nms(hits)

    def _refine(self, gray: np.ndarray, cand: Hit, tpl: np.ndarray, name: str) -> Hit:
        size0 = max(cand.w, cand.h)
        lo, hi = size0 / 1.2, size0 * 1.2
        sizes = sorted({round(x) for x in np.arange(lo, hi + 1, max(1.0, size0 * 0.04))})
        best = cand
        pad = int(size0 * 1.5) + 4
        x0, y0 = int(max(0, cand.x - pad)), int(max(0, cand.y - pad))
        roi = gray[y0:int(cand.y + pad), x0:int(cand.x + pad)]
        for size in sizes:
            for rot in range(4):
                tr = self._template(tpl, size, rot)
                if tr is None or tr.shape[0] >= roi.shape[0] or tr.shape[1] >= roi.shape[1]:
                    continue
                res = cv2.matchTemplate(roi, tr, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx > best.score:
                    th, tw = tr.shape
                    best = Hit(x0 + loc[0] + tw / 2, y0 + loc[1] + th / 2, tw, th, float(mx), cand.label)
        self._note_best(best.score, max(best.w, best.h), name, best.x, best.y, best.w, best.h)
        return best

    def _note_best(self, score, size, name, x, y, w, h) -> None:
        if self.best is None or score > self.best["score"]:
            self.best = {"score": float(score), "size_px": round(size), "reference": name, "x": x, "y": y, "w": w, "h": h}


class YoloDetector:
    def __init__(self, weights: Path):
        from ultralytics import YOLO  # optional dependency

        self.model = YOLO(str(weights))

    best = None

    def detect(self, frame: np.ndarray, threshold: float, expected_px: float | None, label: str,
               frame_scale: float = 1.0) -> list[Hit]:
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


def _save_crop(frame: np.ndarray, x: float, y: float, w: float, h: float) -> str | None:
    pad = max(w, h)
    x0, y0 = int(max(0, x - pad)), int(max(0, y - pad))
    crop = frame[y0:int(y + pad), x0:int(x + pad)]
    if not crop.size:
        return None
    name = f"{uuid.uuid4().hex[:16]}.jpg"
    cv2.imwrite(str(config.CROP_DIR / name), crop)
    return name


def run_job(job_id: str, video: Path, srt_path: Path | None, opts: Options, user_id: int | None) -> None:
    try:
        _update_job(job_id, status="running", message="Loading detector")
        detector = load_detector()
        fixes = []
        if srt_path:
            text = srt_path.read_text(errors="ignore")
            fixes = srt.parse(text)
            if not fixes and (opts.lat is None or opts.lng is None):
                sample = next((ln.strip() for ln in text.splitlines() if "[" in ln or "GPS" in ln), text[:120])
                raise ValueError(
                    "Could not read GPS positions from the .srt file. Its lines look like: "
                    f"'{sample[:160]}'. Send this line to the developer, or enter the camera position "
                    "and height under Advanced."
                )
        if not fixes and (opts.lat is None or opts.lng is None):
            raise ValueError("No .srt file: enter the camera position and height under Advanced")
        if not fixes and opts.altitude_m is None:
            raise ValueError("Give the flight height under Advanced when there is no .srt file")

        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            raise ValueError("Could not open the video file")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        step = max(1, int(round(fps * opts.sample_every_s)))
        stats = {"read": 0, "checked": 0, "no_alt": 0, "best": None}
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
                stats["read"] += 1
                n_hits += _process_frame(frame, idx / fps, fixes, detector, opts, points, stats)
                if frames:
                    _update_job(job_id, progress=min(0.99, idx / frames), detections=n_hits,
                                message=f"Frame {idx}/{frames}")
            idx += 1
        cap.release()

        if stats["read"] == 0:
            raise ValueError(
                "No frames could be read from the video. DJI H.265 (HEVC) or 10-bit D-Log files sometimes "
                "can't be decoded; convert it first: ffmpeg -i input.mp4 -c:v libx264 -crf 18 output.mp4"
            )
        if stats["checked"] == 0 and stats["no_alt"]:
            raise ValueError(
                f"All {stats['no_alt']} sampled frames were skipped: the .srt has no flight height "
                "(rel_alt / altitude), or it is 0 (drone on the ground). Enter the height above ground "
                "under Advanced → Altitude."
            )

        clusters = [c for c in cluster(points, opts.merge_radius_m) if c.hits >= opts.min_hits]
        saved = save_markers(clusters, source=video.name, user_id=user_id)
        best = stats["best"]
        fields = {"frames_checked": stats["checked"], "frames_skipped": stats["no_alt"]}
        if best:
            fields.update(best_score=round(best["score"], 3), best_time=round(best["t"], 1),
                          best_size_px=best["size_px"], best_crop=best["crop"])
        skipped = f" {stats['no_alt']} frame(s) skipped for lack of height." if stats["no_alt"] else ""
        if saved:
            msg = f"{saved} object(s) marked from {n_hits} detection(s) in {stats['checked']} frame(s).{skipped}"
        elif best:
            suggest = max(0.3, round(best["score"] - 0.03, 2))
            msg = (f"No objects found in {stats['checked']} frame(s). Best match {best['score']:.2f} "
                   f"(threshold {opts.threshold}) at {best['t']:.0f} s, about {best['size_px']} px. "
                   f"If the picture shows the object, set the match threshold to about {suggest}"
                   f"{' and Min. frames to 1' if opts.min_hits > 1 else ''}.{skipped}")
        else:
            msg = f"No objects found in {stats['checked']} frame(s).{skipped}"
        _update_job(job_id, status="done", progress=1.0, detections=n_hits, markers=saved, message=msg, **fields)
    except Exception as e:
        log.exception("Detection job %s failed", job_id)
        _update_job(job_id, status="error", message=str(e))


def _process_frame(frame, t, fixes, detector, opts: Options, points: list, stats: dict | None = None) -> int:
    stats = stats if stats is not None else {"read": 0, "checked": 0, "no_alt": 0, "best": None}
    if fixes:
        fix = srt.fix_at(fixes, t)
        lat, lng, yaw = fix.lat, fix.lng, fix.yaw
        alt = opts.altitude_m if opts.altitude_m is not None else fix.alt
    else:
        lat, lng, yaw, alt = opts.lat, opts.lng, None, opts.altitude_m
    if not alt or alt <= 0:
        stats["no_alt"] += 1
        return 0  # on the ground / no altitude: cannot geo-locate
    stats["checked"] += 1

    h0, w0 = frame.shape[:2]
    h, w = h0, w0
    if w > opts.max_width:
        s = opts.max_width / w
        frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]

    expected_px = None
    if opts.object_size_m:
        expected_px = opts.object_size_m / metres_per_pixel(alt, opts.hfov_deg, w)
    before = (detector.best or {}).get("score")
    hits = detector.detect(frame, opts.threshold, expected_px, opts.label, frame_scale=w / w0)
    b = detector.best
    if b and b.get("score") != before and (stats["best"] is None or b["score"] > stats["best"]["score"]):
        stats["best"] = {"score": b["score"], "t": t, "size_px": b["size_px"],
                         "crop": _save_crop(frame, b["x"], b["y"], b["w"], b["h"])}
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
