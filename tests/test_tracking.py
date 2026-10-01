from app.geo import from_local, haversine, project_on_segment

START = (33.6000, 73.0000)
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
    assert s["grid"]["start"] and s["grid"]["start"].startswith("43")
    a = from_local(*START, -300, -400)  # point A, 500 m from start
    client.post("/api/tracking/position", json={"lat": a[0], "lng": a[1]})
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
