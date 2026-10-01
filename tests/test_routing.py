import sqlite3

import httpx

from app import config, routing
from app.geo import haversine

ORIGIN = (33.6000, 73.0000)
DEST = (33.6100, 73.0200)

# A trimmed OSRM response: two roads, the destination is 120 m off-road.
OSRM = {
    "code": "Ok",
    "waypoints": [
        {"location": [73.0000, 33.6000], "distance": 2.0},
        {"location": [73.0190, 33.6100], "distance": 92.6},
    ],
    "routes": [{
        "distance": 2100.0,
        "duration": 180.0,
        "geometry": {"type": "LineString", "coordinates": [[73.0, 33.6], [73.0, 33.61], [73.019, 33.61]]},
        "legs": [{"steps": [
            {"distance": 1100.0, "duration": 90.0, "name": "Canal Road",
             "maneuver": {"type": "depart", "bearing_after": 0, "location": [73.0, 33.6]}},
            {"distance": 1000.0, "duration": 90.0, "name": "",
             "maneuver": {"type": "turn", "modifier": "right", "location": [73.0, 33.61]}},
            {"distance": 0.0, "duration": 0.0, "name": "",
             "maneuver": {"type": "arrive", "location": [73.019, 33.61]}},
        ]}],
    }],
}


def test_parse_osrm_adds_offroad_leg():
    r = routing.parse_osrm(OSRM, ORIGIN, DEST)
    texts = [s["text"] for s in r["steps"]]
    assert texts[0] == "Head north onto Canal Road"
    assert texts[1] == "Turn right"
    assert texts[2].startswith("Leave the road: head east")
    assert texts[-1] == "Arrive at the start point"
    assert r["geometry"][-1] == list(DEST)
    assert r["distance_m"] > 2100 and r["source"] == "osrm"


def test_instruction_texts():
    assert routing.instruction({"maneuver": {"type": "roundabout", "exit": 2}, "name": "GT Road"}) == \
        "At the roundabout, take the 2nd exit onto GT Road"
    assert routing.instruction({"maneuver": {"type": "fork", "modifier": "slight left"}}) == "Keep slight left at the fork"
    assert routing.instruction({"maneuver": {"type": "end of road", "modifier": "left"}}) == \
        "At the end of the road, turn left"


def test_fallback_without_osrm(client, monkeypatch):
    monkeypatch.setattr(config, "OSRM_URL", "")
    r = client.get("/api/route", params={"from_lat": ORIGIN[0], "from_lng": ORIGIN[1],
                                         "to_lat": DEST[0], "to_lng": DEST[1]}).json()
    assert r["source"] == "straight-line" and "MDS_OSRM_URL" in r["warning"]
    assert abs(r["distance_m"] - haversine(*ORIGIN, *DEST)) < 0.01


def test_osrm_called_and_unreachable_falls_back(client, monkeypatch):
    monkeypatch.setattr(config, "OSRM_URL", "http://osrm.local:5000")
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return httpx.Response(200, json=OSRM)

    monkeypatch.setattr(routing.httpx, "get", fake_get)
    q = {"from_lat": ORIGIN[0], "from_lng": ORIGIN[1], "to_lat": DEST[0], "to_lng": DEST[1]}
    r = client.get("/api/route", params=q).json()
    assert r["source"] == "osrm"
    assert calls[0] == "http://osrm.local:5000/route/v1/driving/73.0,33.6;73.02,33.61"

    def down(url, params, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(routing.httpx, "get", down)
    r = client.get("/api/route", params=q).json()
    assert r["source"] == "straight-line" and "unreachable" in r["warning"]


def test_mbtiles_served_offline(client, monkeypatch, tmp_path):
    path = tmp_path / "map.mbtiles"
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB);
        INSERT INTO metadata VALUES ('format', 'jpg');""")
    conn.execute("INSERT INTO tiles VALUES (3, 5, 7 - 2, ?)", (b"JPEGDATA",))  # TMS row for y=2
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "MBTILES", path)
    assert client.get("/api/config").json()["tile_url"] == "/tiles/{z}/{x}/{y}"
    r = client.get("/tiles/3/5/2")
    assert r.status_code == 200 and r.content == b"JPEGDATA" and r.headers["content-type"] == "image/jpeg"
    assert client.get("/tiles/3/5/3").status_code == 404
