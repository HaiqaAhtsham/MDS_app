"""Road routing through a self-hosted OSRM server.

The approach leg (vehicle -> start point) follows roads. OSRM snaps both
ends to the nearest road, so when the start point is off-road (as a strip
usually is) a final cross-country segment is added from the road to the
point. The laying leg (start -> end) is the straight strip centreline and
is handled by the client.

If MDS_OSRM_URL is unset or the server can't be reached, a straight-line
route is returned and flagged, so the UI can say there is no road routing.
"""

import math

import httpx

from . import config
from .geo import bearing, haversine

# Gap between the requested point and the road OSRM snapped it to, above
# which an explicit cross-country step is added.
OFFROAD_STEP_M = 15

_COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


def compass(deg: float) -> str:
    return _COMPASS[int((deg + 22.5) // 45) % 8]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def instruction(step: dict) -> str:
    """Human text for one OSRM step."""
    m = step.get("maneuver", {})
    kind, mod = m.get("type", ""), m.get("modifier", "")
    road = step.get("name") or step.get("ref") or ""
    onto = f" onto {road}" if road else ""
    if kind == "depart":
        return f"Head {compass(m.get('bearing_after', 0))}{onto}"
    if kind == "arrive":
        return "Arrive at the road point nearest the start point"
    if kind in ("roundabout", "rotary", "roundabout turn"):
        exit_n = m.get("exit")
        return f"At the roundabout, take the {_ordinal(exit_n)} exit{onto}" if exit_n else f"Go through the roundabout{onto}"
    if kind in ("exit roundabout", "exit rotary"):
        return f"Exit the roundabout{onto}"
    if mod == "uturn":
        return f"Make a U-turn{onto}"
    if kind in ("continue", "new name"):
        return f"Continue {mod or 'straight'}{onto}" if mod and mod != "straight" else f"Continue straight{onto}"
    if kind == "merge":
        return f"Merge {mod}{onto}".replace("  ", " ")
    if kind in ("on ramp", "off ramp"):
        return f"Take the ramp {mod}{onto}".replace("  ", " ")
    if kind == "fork":
        return f"Keep {mod} at the fork{onto}"
    if kind == "end of road":
        return f"At the end of the road, turn {mod}{onto}"
    if mod == "straight":
        return f"Go straight{onto}"
    return f"Turn {mod}{onto}" if mod else f"Continue{onto}"


def _offroad_step(a, b, text_prefix: str) -> dict:
    d = haversine(*a, *b)
    brg = bearing(a, b)
    return {
        "text": f"{text_prefix}: head {compass(brg)} ({brg:.0f}°) for {d:.0f} m",
        "type": "offroad",
        "modifier": "",
        "distance_m": d,
        "duration_s": None,
        "location": list(a),
    }


def straight_route(origin, dest, reason: str) -> dict:
    d = haversine(*origin, *dest)
    return {
        "source": "straight-line",
        "warning": f"No road routing ({reason}). Showing a straight line; follow tracks with care.",
        "distance_m": d,
        "duration_s": None,
        "geometry": [list(origin), list(dest)],
        "steps": [
            _offroad_step(origin, dest, "Cross-country"),
            {"text": "Arrive at the start point", "type": "arrive", "modifier": "", "distance_m": 0,
             "duration_s": None, "location": list(dest)},
        ],
    }


def parse_osrm(data: dict, origin, dest) -> dict:
    """Turn an OSRM /route response into our route shape."""
    route = data["routes"][0]
    geometry = [[lat, lng] for lng, lat in route["geometry"]["coordinates"]]
    steps = []
    for leg in route["legs"]:
        for s in leg["steps"]:
            lng, lat = s["maneuver"]["location"]
            steps.append({
                "text": instruction(s),
                "type": s["maneuver"].get("type", ""),
                "modifier": s["maneuver"].get("modifier", ""),
                "distance_m": s.get("distance", 0.0),
                "duration_s": s.get("duration", 0.0),
                "location": [lat, lng],
            })
    distance, duration = route["distance"], route["duration"]

    # Cross-country bits between the requested points and the snapped road.
    wps = data.get("waypoints", [])
    if wps and wps[0].get("distance", 0) > OFFROAD_STEP_M:
        snap = [wps[0]["location"][1], wps[0]["location"][0]]
        first = _offroad_step(origin, snap, "Go to the road")
        steps.insert(0, first)
        geometry.insert(0, list(origin))
        distance += first["distance_m"]
    if len(wps) > 1 and wps[-1].get("distance", 0) > OFFROAD_STEP_M:
        snap = [wps[-1]["location"][1], wps[-1]["location"][0]]
        last = _offroad_step(snap, dest, "Leave the road")
        # Replace OSRM's "arrive" with the cross-country step + a real arrival.
        if steps and steps[-1]["type"] == "arrive":
            steps.pop()
        steps.append(last)
        geometry.append(list(dest))
        distance += last["distance_m"]
    if steps and steps[-1]["type"] == "arrive":
        steps.pop()
    steps.append({"text": "Arrive at the start point", "type": "arrive", "modifier": "", "distance_m": 0,
                  "duration_s": 0, "location": list(dest)})
    return {
        "source": "osrm",
        "warning": None,
        "distance_m": distance,
        "duration_s": duration,  # road part only; cross-country time is unknown
        "geometry": geometry,
        "steps": steps,
    }


def route(origin: tuple[float, float], dest: tuple[float, float]) -> dict:
    if not config.OSRM_URL:
        return straight_route(origin, dest, "MDS_OSRM_URL is not set")
    url = (
        f"{config.OSRM_URL.rstrip('/')}/route/v1/{config.OSRM_PROFILE}/"
        f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    )
    try:
        r = httpx.get(url, params={"steps": "true", "geometries": "geojson", "overview": "full"}, timeout=10)
        data = r.json()
        if r.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
            return straight_route(origin, dest, f"OSRM: {data.get('message') or data.get('code') or r.status_code}")
        return parse_osrm(data, origin, dest)
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        return straight_route(origin, dest, f"routing server unreachable: {type(e).__name__}")
