"""Vehicle tracking and path progress.

The mission has two legs:
  1. approach: from wherever the vehicle was when the mission started
     (point A) to the marked start point;
  2. strip: from the start point to the end point. This is the leg whose
     progress fills the "Minefield Progress" ring.

Progress on the strip is the vehicle's GPS fix projected onto the
start->end line. We keep the furthest projection reached so far
("covered"), so GPS jitter does not make the ring go backwards.
"""

import time

from . import config, db
from .geo import haversine, project_on_segment

TRAIL_LIMIT = 500


def _mission(c) -> dict:
    return dict(c.execute("SELECT * FROM mission WHERE id = 1").fetchone())


def _latest(c) -> dict | None:
    row = c.execute("SELECT lat, lng, accuracy, heading, speed, ts FROM positions ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def set_route(start: tuple[float, float], end: tuple[float, float]) -> None:
    with db.tx() as c:
        c.execute(
            """UPDATE mission SET start_lat = ?, start_lng = ?, end_lat = ?, end_lng = ?,
               origin_lat = NULL, origin_lng = NULL, reached_start = 0, max_progress = 0,
               status = 'planned', started_at = NULL WHERE id = 1""",
            (*start, *end),
        )


def start_mission() -> dict:
    with db.tx() as c:
        m = _mission(c)
        if m["start_lat"] is None or m["end_lat"] is None:
            raise ValueError("Mark a start and end point first")
        pos = _latest(c)
        origin = (pos["lat"], pos["lng"]) if pos and time.time() - pos["ts"] < 300 else (None, None)
        c.execute(
            """UPDATE mission SET status = 'approach', origin_lat = ?, origin_lng = ?,
               reached_start = 0, max_progress = 0, started_at = ? WHERE id = 1""",
            (*origin, time.time()),
        )
        if origin[0] is not None:
            _advance(c, origin[0], origin[1])
    return state()


def reset_mission() -> None:
    with db.tx() as c:
        c.execute(
            """UPDATE mission SET start_lat = NULL, start_lng = NULL, end_lat = NULL, end_lng = NULL,
               origin_lat = NULL, origin_lng = NULL, reached_start = 0, max_progress = 0,
               status = 'idle', started_at = NULL WHERE id = 1"""
        )
        c.execute("DELETE FROM positions")


def record_position(lat: float, lng: float, accuracy=None, heading=None, speed=None, user_id=None) -> dict:
    with db.tx() as c:
        c.execute(
            "INSERT INTO positions (lat, lng, accuracy, heading, speed, user_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lat, lng, accuracy, heading, speed, user_id, time.time()),
        )
        _advance(c, lat, lng)
    return state()


def _advance(c, lat: float, lng: float) -> None:
    """Move the mission state machine forward for a new fix."""
    m = _mission(c)
    if m["status"] not in ("approach", "laying"):
        return
    if m["origin_lat"] is None:
        c.execute("UPDATE mission SET origin_lat = ?, origin_lng = ? WHERE id = 1", (lat, lng))
    start, end = (m["start_lat"], m["start_lng"]), (m["end_lat"], m["end_lng"])

    reached = bool(m["reached_start"])
    if not reached and haversine(lat, lng, *start) <= config.ARRIVAL_RADIUS_M:
        reached = True
    if not reached:
        c.execute("UPDATE mission SET status = 'approach' WHERE id = 1")
        return

    frac, _, _ = project_on_segment(start, end, (lat, lng))
    covered = max(m["max_progress"], frac)
    status = "laying"
    if haversine(lat, lng, *end) <= config.ARRIVAL_RADIUS_M:
        covered, status = 1.0, "complete"
    c.execute(
        "UPDATE mission SET reached_start = 1, max_progress = ?, status = ? WHERE id = 1",
        (covered, status),
    )


def state() -> dict:
    with db.tx() as c:
        m = _mission(c)
        pos = _latest(c)
        since = m["started_at"] or 0
        trail = [
            [r["lat"], r["lng"]]
            for r in c.execute(
                "SELECT lat, lng FROM (SELECT id, lat, lng FROM positions WHERE ts >= ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (since, TRAIL_LIMIT),
            )
        ]

    out = {
        "status": m["status"],
        "start": [m["start_lat"], m["start_lng"]] if m["start_lat"] is not None else None,
        "end": [m["end_lat"], m["end_lng"]] if m["end_lat"] is not None else None,
        "origin": [m["origin_lat"], m["origin_lng"]] if m["origin_lat"] is not None else None,
        "vehicle": pos,
        "trail": trail,
        "covered": round(m["max_progress"], 4),
        "strip_length_m": None,
        "approach": None,
        "strip": None,
    }
    if not out["start"] or not out["end"]:
        return out
    start, end = tuple(out["start"]), tuple(out["end"])
    out["strip_length_m"] = round(haversine(*start, *end), 1)
    if not pos:
        return out

    here = (pos["lat"], pos["lng"])
    to_start = haversine(*here, *start)
    if out["origin"]:
        total = haversine(*out["origin"], *start)
        approach_frac = 1.0 if total <= config.ARRIVAL_RADIUS_M else max(0.0, min(1.0, 1 - to_start / total))
    else:
        approach_frac = None
    out["approach"] = {
        "distance_to_start_m": round(to_start, 1),
        "progress": 1.0 if m["reached_start"] else approach_frac,
    }
    frac, cross, length = project_on_segment(start, end, here)
    out["strip"] = {
        "current": round(frac, 4),
        "cross_track_m": round(cross, 1),
        "off_route": bool(m["reached_start"]) and m["status"] != "complete" and cross > config.OFF_ROUTE_M,
        "remaining_m": round(length * (1 - m["max_progress"]), 1),
    }
    return out
