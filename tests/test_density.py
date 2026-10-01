import pytest

from app import density

# The chart exactly as printed (Annexure A, p. 120), column by column.
AT = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1]
ATM = [0.16, 0.24, 0.32, 0.4, 0.48, 0.56, 0.64, 0.72, 0.8]
APM = [0.12, 0.18, 0.24, 0.3, 0.36, 0.42, 0.48, 0.54, 0.6]
FRONTAGE = [7200, 4800, 3456, 2880, 2390, 2044, 1814, 1613, 1440]
INTERVAL = [25, 16.7, 12, 10, 8.3, 7.1, 6.3, 5.6, 5]
SPEED = {
    1: [90, 60, 43.2, 36, 29.9, 25.6, 22.6, 20, 18],
    2: [45, 30, 21.6, 18, 15, 12.8, 11.3, 10, 9],
    5: [18, 12, 8.6, 6.1, 5.6, 5.1, 4.5, 4, 3.6],
}


@pytest.mark.parametrize("i", range(9))
@pytest.mark.parametrize("gap", [1, 2, 5])
def test_every_cell_matches_chart(i, gap):
    at = density.calculate("at", gap, AT[i])
    mixed = density.calculate("mixed", gap, ATM[i], APM[i])
    for r in (at, mixed):
        assert r["column"] == i + 1
        assert r["frontage_m"] == FRONTAGE[i]
        assert r["interval_m"] == INTERVAL[i]
        assert r["required_speed_kmh"] == SPEED[gap][i]


def test_inconsistent_cells_are_flagged_not_changed():
    for atm, printed, computed in ((0.5, 6.1, 7.2), (0.6, 5.6, 5.98)):
        r = density.calculate("at", 5, atm)
        assert r["required_speed_kmh"] == printed  # chart value is used
        speed_check = r["checks"][0]
        assert not speed_check["ok"] and speed_check["computed"] == computed
        assert any("does not match" in f for f in r["flags"])
    # Every other cell is consistent.
    clean = [density.calculate("at", g, a) for g in (1, 2, 5) for a in AT if not (g == 5 and a in (0.5, 0.6))]
    assert all(c["ok"] for r in clean for c in r["checks"])


def test_speed_above_vehicle_maximum_flagged():
    r = density.calculate("at", 1, 0.2)
    assert r["required_speed_kmh"] == 90
    assert any("81 km/hr" in f for f in r["flags"])
    assert not any("81 km/hr" in f for f in density.calculate("at", 1, 0.3)["flags"])


@pytest.mark.parametrize("args", [
    ("at", 3, 0.5, None),  # time gap not in chart
    ("at", 1, 0.25, None),  # density not in chart
    ("mixed", 1, 0.16, 0.18),  # ATM/APM from different columns
    ("mixed", 1, 0.16, None),
    ("mixed", 1, 0.2, 0.12),  # 0.2 is an anti-tank density, not mixed ATM
    ("other", 1, 0.2, None),
])
def test_rejects_values_not_in_chart(args):
    with pytest.raises(density.SelectionError):
        density.calculate(*args)


def test_api(client):
    t = client.get("/api/density").json()
    assert t["anti_tank"] == AT and t["time_gaps"] == [1, 2, 5]
    r = client.post("/api/density/calc", json={"mine_type": "mixed", "atm": 0.64, "apm": 0.48, "time_gap": 1})
    assert r.json()["frontage_m"] == 1814 and r.json()["required_speed_kmh"] == 22.6
    r = client.post("/api/density/calc", json={"mine_type": "mixed", "atm": 0.64, "apm": 0.6, "time_gap": 1})
    assert r.status_code == 400 and "only with APM 0.48" in r.json()["detail"]
