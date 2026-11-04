"""Road routing through OSRM: a self-hosted server, or the public
OpenStreetMap servers when there is none.

The approach leg (vehicle -> start point) follows roads. OSRM snaps both
ends to the nearest road, so when the start point is off-road (as a strip
usually is) a final cross-country segment is added from the road to the
point. The laying leg (start -> end) is the straight strip centreline and
is handled by the client.

Two modes: "roads" (car profile, paved roads) and "tracks" (a profile that
also follows unpaved tracks). If no server answers, a straight-line route
is returned and flagged, so the UI can say there is no road routing.
"""

import math

import httpx

from . import config
from .geo import bearing, haversine

MODES = ("roads", "tracks")
USER_AGENT = "MDS-Dashboard/1.0 (minefield laying navigation)"

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


def servers(mode: str) -> list[tuple[str, str, str]]:
    """(base url, profile, label) to try in order for a mode."""
    out = []
    if mode == "tracks":
        if config.OSRM_TRACKS_URL:
            out.append((config.OSRM_TRACKS_URL, config.OSRM_PROFILE, "local"))
        if config.ONLINE_ROUTING:
            out.append((config.PUBLIC_OSRM["tracks"], "driving", "online"))
    if config.OSRM_URL:
        out.append((config.OSRM_URL, config.OSRM_PROFILE, "local"))
    if config.ONLINE_ROUTING:
        out.append((config.PUBLIC_OSRM["roads"], "driving", "online"))
    return out


def _ask(base: str, profile: str, origin, dest) -> dict:
    url = f"{base.rstrip('/')}/route/v1/{profile}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    r = httpx.get(url, params={"steps": "true", "geometries": "geojson", "overview": "full"},
                  headers={"User-Agent": USER_AGENT}, timeout=10)
    data = r.json()
    if r.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
        raise LookupError(data.get("message") or data.get("code") or str(r.status_code))
    return data


def route(origin: tuple[float, float], dest: tuple[float, float], mode: str = "roads") -> dict:
    mode = mode if mode in MODES else "roads"
    tried = servers(mode)
    if not tried:
        return straight_route(origin, dest, "no routing server: set MDS_OSRM_URL or allow online routing")
    track_servers = {u for u in (config.OSRM_TRACKS_URL, config.PUBLIC_OSRM["tracks"]) if u}
    problems = []
    for base, profile, label in tried:
        try:
            out = parse_osrm(_ask(base, profile, origin, dest), origin, dest)
        except LookupError as e:
            problems.append(f"{label} server: {e}")
            continue
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            problems.append(f"{label} routing server unreachable ({type(e).__name__})")
            continue
        via_tracks = mode == "tracks" and base in track_servers
        out["mode"] = "tracks" if via_tracks else "roads"
        out["server"] = label
        if via_tracks:  # the tracks profile's own times are not for a vehicle
            speed = config.TRACK_SPEED_KMH / 3.6
            out["duration_s"] = out["distance_m"] / speed
            for st in out["steps"]:
                st["duration_s"] = st["distance_m"] / speed if st["distance_m"] else 0
        elif mode == "tracks":
            out["warning"] = "Tracks routing is unavailable; showing a roads-only route."
        return out
    return straight_route(origin, dest, "; ".join(problems))
