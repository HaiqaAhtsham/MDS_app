"""Parser for drone telemetry subtitle files (.srt).

DJI drones write one subtitle cue per frame (or per second), and the cue
text carries the GPS fix and altitude. Firmwares disagree on the format, so
this handles the common variants:

  [latitude: 33.684422] [longitude: 73.047882] [rel_alt: 50.000 abs_alt: 560.0]
  [latitude : 33.68] [longtitude : 73.04] [altitude: 50.0]       (Mini series)
  GPS(73.047882,33.684422,19) BAROMETER:50.2                       (older models)
  [gb_yaw: -10.2 gb_pitch: -90.0 gb_roll: 0.0]                     (gimbal)
"""

import re
from dataclasses import dataclass

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_KV = re.compile(r"([A-Za-z_]+)\s*:\s*(-?\d+(?:\.\d+)?)")
_GPS_OLD = re.compile(r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")
_BARO = re.compile(r"BAROMETER\s*:?\s*(-?\d+(?:\.\d+)?)")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Fix:
    start: float  # seconds into the video
    end: float
    lat: float
    lng: float
    alt: float | None = None  # metres above take-off point
    yaw: float | None = None  # degrees clockwise from north
    pitch: float | None = None  # gimbal pitch, -90 = straight down
    focal_len: float | None = None


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))


def parse(text: str) -> list[Fix]:
    fixes: list[Fix] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        m = _TIME.search(block)
        if not m:
            continue
        g = m.groups()
        start, end = _secs(*g[:4]), _secs(*g[4:])
        body = _TAG.sub(" ", block[m.end():])
        kv = {k.lower(): float(v) for k, v in _KV.findall(body)}

        lat = kv.get("latitude")
        lng = kv.get("longitude", kv.get("longtitude"))
        alt = kv.get("rel_alt", kv.get("altitude", kv.get("h")))
        old = _GPS_OLD.search(body)
        if (lat is None or lng is None) and old:
            lng, lat = float(old.group(1)), float(old.group(2))
            baro = _BARO.search(body)
            alt = float(baro.group(1)) if baro else alt
        if lat is None or lng is None or (lat == 0 and lng == 0):
            continue  # no GPS lock for this cue
        yaw = kv.get("gb_yaw", kv.get("yaw", kv.get("drone_yaw")))
        fixes.append(Fix(start, end, lat, lng, alt, yaw, kv.get("gb_pitch", kv.get("pitch")), kv.get("focal_len")))
    fixes.sort(key=lambda f: f.start)
    return fixes


def fix_at(fixes: list[Fix], t: float) -> Fix | None:
    """Telemetry for time t (seconds). Falls back to the nearest cue."""
    if not fixes:
        return None
    lo, hi = 0, len(fixes) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fixes[mid].start <= t:
            lo = mid
        else:
            hi = mid - 1
    best = fixes[lo]
    if lo + 1 < len(fixes) and abs(fixes[lo + 1].start - t) < abs(best.start - t):
        best = fixes[lo + 1]
    return best
