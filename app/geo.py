"""Small-area geodesy helpers. Distances in this app are at most a few km,
so a local equirectangular projection around a reference point is accurate
to well under a metre."""

import math

EARTH_R = 6_371_000.0


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def to_local(lat0: float, lng0: float, lat: float, lng: float) -> tuple[float, float]:
    """(east, north) metres of (lat, lng) relative to (lat0, lng0)."""
    east = math.radians(lng - lng0) * EARTH_R * math.cos(math.radians(lat0))
    north = math.radians(lat - lat0) * EARTH_R
    return east, north


def from_local(lat0: float, lng0: float, east: float, north: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(north / EARTH_R)
    lng = lng0 + math.degrees(east / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lng


def project_on_segment(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> tuple[float, float, float]:
    """Project point p onto segment a->b (all (lat, lng)).

    Returns (fraction along the segment clamped to 0..1, cross-track distance
    in metres, segment length in metres)."""
    bx, by = to_local(a[0], a[1], b[0], b[1])
    px, py = to_local(a[0], a[1], p[0], p[1])
    length_sq = bx * bx + by * by
    if length_sq == 0:
        return 1.0, math.hypot(px, py), 0.0
    t = (px * bx + py * by) / length_sq
    t_clamped = min(1.0, max(0.0, t))
    cross = math.hypot(px - t_clamped * bx, py - t_clamped * by)
    return t_clamped, cross, math.sqrt(length_sq)


def bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial compass bearing from a to b, degrees clockwise from north."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat: float, lng: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """The point distance_m from (lat, lng) along bearing_deg (great circle)."""
    d = distance_m / EARTH_R
    t = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lng)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(t))
    l2 = l1 + math.atan2(math.sin(t) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180
