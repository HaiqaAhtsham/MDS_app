"""Canister stock and automatic firing count.

The handout (p. 27) lists the two canister types in service:
  GBL-240: 5 x anti-tank mines           -> used for anti-tank minefields
  GBL-243: 4 x anti-tank + 3 x AP mines  -> used for mixed minefields

While the vehicle is laying (from the start point to the end point) the
MLCU fires one canister every "time gap" seconds, the interval chosen in
the minefield parameters. Fired canisters are taken from the loaded ones;
firing stops when that type runs out.
"""

from . import config
from .db import CANISTER_TYPES

CANISTER_FOR = {"at": "GBL-240", "mixed": "GBL-243"}


def _key(field: str, ctype: str) -> str:
    return f"ammo_{field}_{ctype[4:]}"


def _get(c, key: str) -> int:
    row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return int(float(row["value"])) if row else 0


def _set(c, key: str, value: int) -> None:
    c.execute(
        "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(int(value))),
    )


def read(c) -> dict:
    types = {}
    for t in CANISTER_TYPES:
        total, loaded, fired = (_get(c, _key(f, t)) for f in ("total", "loaded", "fired"))
        types[t] = {"total": total, "loaded": loaded, "fired": fired, "remaining": max(0, loaded - fired)}
    return {
        "types": types,
        "total": sum(v["total"] for v in types.values()),
        "loaded": sum(v["loaded"] for v in types.values()),
        "fired": sum(v["fired"] for v in types.values()),
        "max_loaded": config.CANISTERS_PER_SALVO,
    }


class AmmoError(ValueError):
    pass


def update(c, total: dict, loaded: dict, reset_fired: bool) -> dict:
    """total / loaded: {"GBL-240": n, "GBL-243": n}."""
    for t in CANISTER_TYPES:
        if total[t] < 0 or loaded[t] < 0:
            raise AmmoError("Numbers can't be negative")
        if loaded[t] > total[t]:
            raise AmmoError(f"{t}: loaded ({loaded[t]}) can't be more than total ({total[t]})")
        fired = 0 if reset_fired else _get(c, _key("fired", t))
        if fired > loaded[t]:
            raise AmmoError(f"{t}: {fired} already fired, so loaded can't be less than that (or reset the fired count)")
    if sum(loaded.values()) > config.CANISTERS_PER_SALVO:
        raise AmmoError(f"The launchers hold {config.CANISTERS_PER_SALVO} canisters in total (8 boxes x 36)")
    for t in CANISTER_TYPES:
        _set(c, _key("total", t), total[t])
        _set(c, _key("loaded", t), loaded[t])
        if reset_fired:
            _set(c, _key("fired", t), 0)
    return read(c)


def fire(c, m: dict, now: float) -> None:
    """Count the canisters fired since the last call, one per time gap."""
    if m["status"] != "laying" or m.get("fire_next_at") is None or not m.get("time_gap"):
        return
    gap = float(m["time_gap"])
    if now < m["fire_next_at"]:
        return
    due = int((now - m["fire_next_at"]) // gap) + 1
    ctype = CANISTER_FOR.get(m["mine_type"])
    if not ctype:
        return
    fired, loaded = _get(c, _key("fired", ctype)), _get(c, _key("loaded", ctype))
    n = min(due, max(0, loaded - fired))
    _set(c, _key("fired", ctype), fired + n)
    if due > n:  # ran out
        c.execute(
            "UPDATE mission SET fire_next_at = NULL, fire_stopped = ? WHERE id = 1",
            (f"Firing stopped: no loaded {ctype} canisters left",),
        )
    else:
        c.execute("UPDATE mission SET fire_next_at = ? WHERE id = 1", (m["fire_next_at"] + due * gap,))
