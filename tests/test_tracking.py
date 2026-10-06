from app.geo import from_local, haversine, project_on_segment

START = (33.6000, 73.0000)


def load_ammo(client, loaded_240=100, loaded_243=100):
    """Enter the ammunition for the planned route (needed before Start)."""
    return client.put("/api/ammo", json={"total": {"GBL-240": 200, "GBL-243": 200},
                                         "loaded": {"GBL-240": loaded_240, "GBL-243": loaded_243}})
END = from_local(*START, 1000, 0)  # 1 km due east


def test_projection_halfway():
    p = from_local(*START, 500, 20)
    frac, cross, length = project_on_segment(START, END, p)
    assert abs(frac - 0.5) < 0.001
    assert abs(cross - 20) < 0.5
    assert abs(length - 1000) < 1


def test_mission_flow(client):
    assert client.post("/api/tracking/start").status_code == 400  # no route yet

    client.post("/api/tracking/point", json={"which": "start", "lat": START[0], "lng": START[1]})
    s = client.post("/api/tracking/point", json={"which": "end", "lat": END[0], "lng": END[1]}).json()
    assert s["status"] == "planned"
    r = client.post("/api/tracking/start")
    assert r.status_code == 400 and "parameters" in r.json()["detail"]  # no mine parameters yet
    s = client.put("/api/tracking/params", json={"mine_type": "at", "atm": 0.5, "time_gap": 2}).json()
    assert s["params"]["frontage_m"] == 2880 and s["params"]["required_speed_kmh"] == 18
    a = from_local(*START, -300, -400)  # point A, 500 m from start
    client.post("/api/tracking/position", json={"lat": a[0], "lng": a[1]})
    load_ammo(client)
    s = client.post("/api/tracking/start").json()
    assert s["status"] == "approach"
    assert s["origin"] == [a[0], a[1]]
    assert s["approach"]["progress"] == 0

    # Halfway to the start point: still approaching, strip progress 0.
    mid = from_local(*START, -150, -200)
    s = client.post("/api/tracking/position", json={"lat": mid[0], "lng": mid[1]}).json()
    assert s["status"] == "approach"
    assert abs(s["approach"]["progress"] - 0.5) < 0.01
    assert s["covered"] == 0

    # Reach the start point, then drive 40% of the strip.
    s = client.post("/api/tracking/position", json={"lat": START[0], "lng": START[1]}).json()
    assert s["status"] == "laying"
    p = from_local(*START, 400, 5)
    s = client.post("/api/tracking/position", json={"lat": p[0], "lng": p[1]}).json()
    assert abs(s["covered"] - 0.4) < 0.01
    assert abs(s["strip"]["remaining_m"] - 600) < 5

    # GPS jitter backwards does not reduce the covered distance.
    back = from_local(*START, 380, 0)
    s = client.post("/api/tracking/position", json={"lat": back[0], "lng": back[1]}).json()
    assert abs(s["covered"] - 0.4) < 0.01

    # Far off the centreline is flagged.
    off = from_local(*START, 500, 120)
    s = client.post("/api/tracking/position", json={"lat": off[0], "lng": off[1]}).json()
    assert s["strip"]["off_route"] is True

    s = client.post("/api/tracking/position", json={"lat": END[0], "lng": END[1]}).json()
    assert s["status"] == "complete" and s["covered"] == 1.0


def test_distance_sanity():
    assert abs(haversine(*START, *END) - 1000) < 1


def test_firing_one_canister_per_time_gap(client, monkeypatch):
    from app import tracking

    clock = [1_000_000.0]
    monkeypatch.setattr(tracking.time, "time", lambda: clock[0])
    client.post("/api/tracking/point", json={"which": "start", "lat": START[0], "lng": START[1]})
    client.post("/api/tracking/point", json={"which": "end", "lat": END[0], "lng": END[1]})
    client.put("/api/tracking/params", json={"mine_type": "at", "atm": 0.5, "time_gap": 2})
    load_ammo(client, loaded_240=3, loaded_243=10)
    a = from_local(*START, -200, 0)
    client.post("/api/tracking/position", json={"lat": a[0], "lng": a[1]})
    client.post("/api/tracking/start")
    s = client.get("/api/tracking").json()
    assert s["ammo"]["fired"] == 0 and not s["firing"]["active"]  # nothing fired on the approach

    # Passing the start point fires the first GBL-240 straight away.
    s = client.post("/api/tracking/position", json={"lat": START[0], "lng": START[1]}).json()
    assert s["status"] == "laying" and s["firing"]["active"] and s["firing"]["canister"] == "GBL-240"
    assert s["ammo"]["types"]["GBL-240"]["fired"] == 1

    clock[0] += 3.9  # 2 s gap -> one more
    assert client.get("/api/tracking").json()["ammo"]["types"]["GBL-240"]["fired"] == 2

    clock[0] += 10  # would be 5 more, but only 3 were loaded
    s = client.get("/api/tracking").json()
    t = s["ammo"]["types"]["GBL-240"]
    assert (t["fired"], t["remaining"]) == (3, 0)
    assert not s["firing"]["active"] and "GBL-240" in s["firing"]["stopped"]
    assert s["ammo"]["types"]["GBL-243"]["fired"] == 0


def test_mixed_field_fires_gbl243_and_stops_at_end(client, monkeypatch):
    from app import tracking

    clock = [2_000_000.0]
    monkeypatch.setattr(tracking.time, "time", lambda: clock[0])
    client.post("/api/tracking/point", json={"which": "start", "lat": START[0], "lng": START[1]})
    client.post("/api/tracking/point", json={"which": "end", "lat": END[0], "lng": END[1]})
    client.put("/api/tracking/params", json={"mine_type": "mixed", "atm": 0.4, "apm": 0.3, "time_gap": 5})
    load_ammo(client, loaded_240=0, loaded_243=100)
    client.post("/api/tracking/position", json={"lat": START[0], "lng": START[1]})
    client.post("/api/tracking/start")
    clock[0] += 12  # fires at 0, 5, 10
    s = client.post("/api/tracking/position", json={"lat": END[0], "lng": END[1]}).json()
    assert s["status"] == "complete" and client.get("/api/routes").json()[0]["fired_243"] == 3
    # The 3 fired rockets left the launchers; nothing more fires after the end point.
    clock[0] += 60
    s = client.get("/api/tracking").json()
    assert s["ammo"]["types"]["GBL-243"]["loaded"] == 97 and s["ammo"]["types"]["GBL-243"]["fired"] == 0
    assert client.get("/api/routes").json()[0]["fired_243"] == 3


def test_end_point_from_frontage(client):
    r = client.post("/api/tracking/end-direction", json={"bearing_deg": 90})
    assert r.status_code == 400 and "start point" in r.json()["detail"]
    client.post("/api/tracking/point", json={"which": "start", "lat": START[0], "lng": START[1]})
    r = client.post("/api/tracking/end-direction", json={"bearing_deg": 90})
    assert r.status_code == 400 and "parameters" in r.json()["detail"]

    client.put("/api/tracking/params", json={"mine_type": "at", "atm": 0.5, "time_gap": 2})  # frontage 2880 m
    s = client.post("/api/tracking/end-direction", json={"bearing_deg": 90}).json()
    assert s["status"] == "planned" and s["end_mode"] == "frontage"
    assert abs(s["strip_length_m"] - 2880) < 0.5 and abs(s["frontage_diff_m"]) < 0.5
    assert abs(s["strip_bearing"] - 90) < 0.5

    # Direction from a clicked point: only the direction is used.
    toward = from_local(*START, 100, 100)  # 141 m away, to the north-east
    s = client.post("/api/tracking/end-direction", json={"toward_lat": toward[0], "toward_lng": toward[1]}).json()
    assert abs(s["strip_length_m"] - 2880) < 0.5 and abs(s["strip_bearing"] - 45) < 0.5

    # New parameters -> new frontage -> end point moves with it.
    s = client.put("/api/tracking/params", json={"mine_type": "at", "atm": 1, "time_gap": 2}).json()
    assert abs(s["strip_length_m"] - 1440) < 0.5 and abs(s["strip_bearing"] - 45) < 0.5

    # Moving the start keeps direction and frontage.
    new_start = from_local(*START, 500, 0)
    s = client.post("/api/tracking/point", json={"which": "start", "lat": new_start[0], "lng": new_start[1]}).json()
    assert abs(s["strip_length_m"] - 1440) < 0.5 and abs(s["strip_bearing"] - 45) < 0.5

    # A typed end point is kept as entered, and the difference is reported.
    s = client.post("/api/tracking/point", json={"which": "end", "lat": END[0], "lng": END[1]}).json()
    assert s["end_mode"] == "manual" and abs(s["frontage_diff_m"] - (s["strip_length_m"] - 1440)) < 0.2


def _plan(client, mine_type="at"):
    client.post("/api/tracking/point", json={"which": "start", "lat": START[0], "lng": START[1]})
    client.post("/api/tracking/point", json={"which": "end", "lat": END[0], "lng": END[1]})
    body = {"mine_type": "at", "atm": 0.5, "time_gap": 1} if mine_type == "at" else \
        {"mine_type": "mixed", "atm": 0.4, "apm": 0.3, "time_gap": 1}
    client.put("/api/tracking/params", json=body)


def test_start_needs_ammunition_for_each_route(client):
    _plan(client)
    r = client.post("/api/tracking/start")
    assert r.status_code == 400 and "ammunition" in r.json()["detail"]
    load_ammo(client, loaded_240=0, loaded_243=50)  # ATM-only field fires GBL-240
    r = client.post("/api/tracking/start")
    assert r.status_code == 400 and "GBL-240" in r.json()["detail"]
    load_ammo(client)
    assert client.post("/api/tracking/start").json()["status"] == "approach"

    # Changing the route needs the ammunition entered again.
    client.post("/api/tracking/point", json={"which": "end", "lat": END[0] + 0.001, "lng": END[1]})
    assert client.post("/api/tracking/start").status_code == 400


def test_route_history_and_fired_count_per_route(client, monkeypatch):
    from app import tracking

    clock = [3_000_000.0]
    monkeypatch.setattr(tracking.time, "time", lambda: clock[0])
    # Route 1: lay the whole strip, 1 s gap.
    _plan(client)
    load_ammo(client, loaded_240=100)
    client.post("/api/tracking/position", json={"lat": START[0], "lng": START[1]})
    client.post("/api/tracking/start")
    clock[0] += 4.5  # fired at 0,1,2,3,4
    s = client.post("/api/tracking/position", json={"lat": END[0], "lng": END[1]}).json()
    assert s["status"] == "complete"
    assert client.get("/api/routes").json()[0]["fired_240"] == 5

    # Route 2 (mixed): fired count starts again from 0; abandoned half way.
    assert client.post("/api/tracking/start").status_code == 400  # ammo again for the new route
    _plan(client, "mixed")
    load_ammo(client, loaded_243=80)
    client.post("/api/tracking/position", json={"lat": START[0], "lng": START[1]})
    s = client.post("/api/tracking/start").json()
    assert s["ammo"]["types"]["GBL-240"]["fired"] == 0 and s["ammo"]["types"]["GBL-243"]["fired"] == 1
    clock[0] += 2.5
    client.get("/api/tracking")
    client.post("/api/tracking/reset")

    # Rockets fired on both routes have left the stock and the launchers.
    stock = client.get("/api/ammo").json()["types"]
    assert (stock["GBL-243"]["total"], stock["GBL-243"]["loaded"], stock["GBL-243"]["fired"]) == (197, 77, 0)
    assert (stock["GBL-240"]["total"], stock["GBL-240"]["loaded"]) == (200, 100)  # re-entered for route 2

    hist = client.get("/api/routes").json()
    assert [h["status"] for h in hist] == ["abandoned", "complete"]
    second, first = hist
    assert (first["canister"], first["fired_240"], first["fired_243"], first["loaded_240"]) == ("GBL-240", 5, 0, 100)
    assert first["covered"] == 1.0 and abs(first["strip_length_m"] - 1000) < 1
    assert (second["canister"], second["fired_243"], second["loaded_243"]) == ("GBL-243", 3, 80)
    assert first["username"] == "admin" and first["ended_at"] > first["started_at"]
