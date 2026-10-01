import pytest

from app import mgrs
from app.geo import haversine


@pytest.mark.parametrize("lat,lng,expected", [
    (0, 0, "31N AA 66021 00000"),
    (38.8895, -77.0353, "18S UJ 23478 06483"),
    (33.6844, 73.0479, "43S CT 19050 28873"),
    (-33.8568, 151.2153, "56H LH 34900 52288"),  # values from the `mgrs` reference library
])
def test_known_points(lat, lng, expected):
    assert mgrs.to_mgrs(lat, lng) == expected
    z, sq, e, n = expected.split()
    back = mgrs.from_mgrs(z, sq, e + n)
    assert haversine(lat, lng, back["lat"], back["lng"]) < 1.5  # 1 m truncation


def test_six_figure_with_defaults_is_sw_corner_of_100m_square():
    full = mgrs.parse("43S CT 190 288")
    bare = mgrs.parse("190288", "43S", "CT")
    assert (full["lat"], full["lng"]) == (bare["lat"], bare["lng"])
    assert full["precision_m"] == 100
    assert mgrs.to_mgrs(full["lat"] + 1e-6, full["lng"] + 1e-6, digits=6) == "43S CT 190 288"


@pytest.mark.parametrize("text,gzd,sq,msg", [
    ("12345", "43S", "CT", "even number"),
    ("190288", None, None, "Set the grid zone"),
    ("43S CI 190288", None, None, "Enter a grid reference"),
    ("43S JT 190288", None, None, "does not exist in zone 43"),
    ("hello", "43S", "CT", "Enter a grid reference"),
])
def test_invalid(text, gzd, sq, msg):
    with pytest.raises(mgrs.GridError, match=msg):
        mgrs.parse(text, gzd, sq)


def test_api_and_defaults(client):
    r = client.post("/api/grid/parse", json={"text": "190288"})
    assert r.status_code == 400 and "grid zone" in r.json()["detail"]
    assert client.put("/api/settings/grid", json={"gzd": "61S", "square": "CT"}).status_code == 400
    s = client.put("/api/settings/grid", json={"gzd": "43s", "square": "ct"}).json()
    assert (s["grid_gzd"], s["grid_square"]) == ("43S", "CT")
    r = client.post("/api/grid/parse", json={"text": "190 288"}).json()
    assert abs(r["lat"] - 33.6837) < 0.001 and abs(r["lng"] - 73.0474) < 0.001
