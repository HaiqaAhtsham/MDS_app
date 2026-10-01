"""Density-frontage chart (MDS handout, Annexure A, p. 120).

The table below is the only source for densities, frontages, intervals and
speeds. Nothing is interpolated: a selection either matches a column of the
chart exactly or is rejected.

Things the chart does not say, and how the app handles them:
  * Speed has no unit. Every column satisfies speed = interval / gap * 3.6,
    i.e. km/h, except two cells (see CHECKS); the user confirmed km/h.
  * Each column pairs one ATM density with one APM density for a mixed
    field, so ATM and APM cannot be chosen independently.
  * Only time gaps of 1, 2 and 5 s are listed.
"""

SOURCE = "MDS handout, Annexure A – Density Frontage Chart (p. 120)"
TIME_GAPS = (1, 2, 5)
CANISTERS_PER_SALVO = 288  # handout p. 8: "No of canisters / salvo - 288"
VEHICLE_MAX_KMH = 81  # handout p. 7: "Maximum speed - 81 km/hr"

# One entry per column of the chart, left to right.
COLUMNS = [
    {"at": 0.2, "atm": 0.16, "apm": 0.12, "frontage_m": 7200, "interval_m": 25, "speed_kmh": {1: 90, 2: 45, 5: 18}},
    {"at": 0.3, "atm": 0.24, "apm": 0.18, "frontage_m": 4800, "interval_m": 16.7, "speed_kmh": {1: 60, 2: 30, 5: 12}},
    {"at": 0.4, "atm": 0.32, "apm": 0.24, "frontage_m": 3456, "interval_m": 12, "speed_kmh": {1: 43.2, 2: 21.6, 5: 8.6}},
    {"at": 0.5, "atm": 0.4, "apm": 0.3, "frontage_m": 2880, "interval_m": 10, "speed_kmh": {1: 36, 2: 18, 5: 6.1}},
    {"at": 0.6, "atm": 0.48, "apm": 0.36, "frontage_m": 2390, "interval_m": 8.3, "speed_kmh": {1: 29.9, 2: 15, 5: 5.6}},
    {"at": 0.7, "atm": 0.56, "apm": 0.42, "frontage_m": 2044, "interval_m": 7.1, "speed_kmh": {1: 25.6, 2: 12.8, 5: 5.1}},
    {"at": 0.8, "atm": 0.64, "apm": 0.48, "frontage_m": 1814, "interval_m": 6.3, "speed_kmh": {1: 22.6, 2: 11.3, 5: 4.5}},
    {"at": 0.9, "atm": 0.72, "apm": 0.54, "frontage_m": 1613, "interval_m": 5.6, "speed_kmh": {1: 20, 2: 10, 5: 4}},
    {"at": 1.0, "atm": 0.8, "apm": 0.6, "frontage_m": 1440, "interval_m": 5, "speed_kmh": {1: 18, 2: 9, 5: 3.6}},
]

# Relative tolerance for the consistency checks (table values are rounded).
CHECK_TOLERANCE = 0.01


class SelectionError(ValueError):
    pass


def table() -> dict:
    return {
        "source": SOURCE,
        "time_gaps": list(TIME_GAPS),
        "columns": COLUMNS,
        "anti_tank": [c["at"] for c in COLUMNS],
        "mixed": [{"atm": c["atm"], "apm": c["apm"]} for c in COLUMNS],
    }


def _find(values: list[float], x: float) -> int | None:
    for i, v in enumerate(values):
        if abs(v - x) < 1e-9:
            return i
    return None


def calculate(mine_type: str, time_gap: int, atm: float, apm: float | None = None) -> dict:
    """Look up frontage and vehicle speed for a selection.

    mine_type "at": atm is the anti-tank density (0.2 ... 1).
    mine_type "mixed": atm/apm are the mixed-field densities, which must be
    a pair from the same column.
    """
    if time_gap not in TIME_GAPS:
        raise SelectionError(f"Time gap must be one of {', '.join(map(str, TIME_GAPS))} s (the chart has no other rows)")
    if mine_type == "at":
        col = _find([c["at"] for c in COLUMNS], atm)
        if col is None:
            raise SelectionError(f"{atm} is not an anti-tank density in the chart")
        density_row = "Anti-Tank"
    elif mine_type == "mixed":
        col = _find([c["atm"] for c in COLUMNS], atm)
        if col is None:
            raise SelectionError(f"{atm} is not a mixed-field ATM density in the chart")
        if apm is None or abs(COLUMNS[col]["apm"] - apm) > 1e-9:
            raise SelectionError(
                f"The chart pairs ATM {atm} only with APM {COLUMNS[col]['apm']}; "
                "other combinations are not in the chart"
            )
        density_row = "Mixed – ATM / APM"
    else:
        raise SelectionError("Mine type must be 'at' or 'mixed'")

    c = COLUMNS[col]
    speed = c["speed_kmh"][time_gap]
    interval = c["interval_m"]

    expected_speed = interval / time_gap * 3.6
    expected_frontage = interval * CANISTERS_PER_SALVO
    checks = [
        {
            "name": "Vehicle speed",
            "formula": f"space interval ÷ time gap × 3.6 = {interval} ÷ {time_gap} × 3.6",
            "computed": round(expected_speed, 2),
            "table": speed,
            "ok": abs(expected_speed - speed) <= CHECK_TOLERANCE * speed,
        },
        {
            "name": "Frontage",
            "formula": f"space interval × {CANISTERS_PER_SALVO} canisters = {interval} × {CANISTERS_PER_SALVO}",
            "computed": round(expected_frontage, 1),
            "table": c["frontage_m"],
            "ok": abs(expected_frontage - c["frontage_m"]) <= CHECK_TOLERANCE * c["frontage_m"],
        },
    ]
    flags = [
        f"Chart value {ch['name'].lower()} {ch['table']} does not match {ch['formula']} = {ch['computed']}. "
        "The chart value is used; verify it before laying."
        for ch in checks
        if not ch["ok"]
    ]
    if speed > VEHICLE_MAX_KMH:
        flags.append(f"{speed} km/h is above the vehicle's maximum speed of {VEHICLE_MAX_KMH} km/hr (handout p. 7).")

    return {
        "mine_type": mine_type,
        "atm": c["atm"] if mine_type == "mixed" else c["at"],
        "apm": c["apm"] if mine_type == "mixed" else None,
        "time_gap_s": time_gap,
        "column": col + 1,
        "column_header": f"Anti-Tank {c['at']}",
        "density_row": density_row,
        "frontage_m": c["frontage_m"],
        "interval_m": interval,
        "required_speed_kmh": speed,
        "source": SOURCE,
        "rows_used": [
            f"{density_row}: column {col + 1}",
            f"Frontage of Mfd (m): {c['frontage_m']}",
            f"Space Interval b/w Canister Deployment: {interval}",
            f"Time Gap = {time_gap}s, Vehicle Speed: {speed}",
        ],
        "checks": checks,
        "flags": flags,
    }
