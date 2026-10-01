"""UTM / MGRS conversion on WGS84 (no external dependencies).

Grid references are MGRS: grid zone designator (e.g. 43S), 100 km square
(e.g. CT) and an even number of digits. A six-figure GR such as 123456
locates a 100 m square; as in standard MGRS truncation, the coordinate is
the south-west corner of that square.

Forward/inverse transverse Mercator uses the Krüger series to 3rd order
(sub-millimetre within a UTM zone).
"""

import math
import re

A = 6378137.0
F = 1 / 298.257223563
K0 = 0.9996
FALSE_E = 500_000.0
FALSE_N_SOUTH = 10_000_000.0

_N = F / (2 - F)
_AA = A / (1 + _N) * (1 + _N**2 / 4 + _N**4 / 64)
_ALPHA = (
    _N / 2 - 2 * _N**2 / 3 + 5 * _N**3 / 16,
    13 * _N**2 / 48 - 3 * _N**3 / 5,
    61 * _N**3 / 240,
)
_BETA = (
    _N / 2 - 2 * _N**2 / 3 + 37 * _N**3 / 96,
    _N**2 / 48 + _N**3 / 15,
    17 * _N**3 / 480,
)
_DELTA = (
    2 * _N - 2 * _N**2 / 3 - 2 * _N**3,
    7 * _N**2 / 3 - 8 * _N**3 / 5,
    56 * _N**3 / 15,
)

BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8° bands from 80°S; X is 12° (72–84°N)
COL_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"


class GridError(ValueError):
    pass


def zone_for(lat: float, lng: float) -> int:
    zone = int((lng + 180) // 6) + 1
    if zone > 60:
        zone = 60
    if 56 <= lat < 64 and 3 <= lng < 12:
        zone = 32  # Norway
    if 72 <= lat < 84 and lng >= 0:  # Svalbard
        if lng < 9:
            zone = 31
        elif lng < 21:
            zone = 33
        elif lng < 33:
            zone = 35
        elif lng < 42:
            zone = 37
    return zone


def band_for(lat: float) -> str:
    if not -80 <= lat <= 84:
        raise GridError("MGRS covers latitudes 80°S to 84°N only")
    return BANDS[min(int((lat + 80) // 8), len(BANDS) - 1)]


def to_utm(lat: float, lng: float, zone: int | None = None) -> tuple[int, bool, float, float]:
    """(zone, northern, easting, northing)."""
    zone = zone or zone_for(lat, lng)
    lng0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lng) - lng0
    e = math.sqrt(F * (2 - F))
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi_p = math.atan2(t, math.cos(lam))
    eta_p = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi, eta = xi_p, eta_p
    for j, a in enumerate(_ALPHA, start=1):
        xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    easting = FALSE_E + K0 * _AA * eta
    northing = K0 * _AA * xi
    northern = lat >= 0
    if not northern:
        northing += FALSE_N_SOUTH
    return zone, northern, easting, northing


def from_utm(zone: int, northern: bool, easting: float, northing: float) -> tuple[float, float]:
    if not northern:
        northing -= FALSE_N_SOUTH
    xi = northing / (K0 * _AA)
    eta = (easting - FALSE_E) / (K0 * _AA)
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_BETA, start=1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    chi = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    phi = chi
    for j, d in enumerate(_DELTA, start=1):
        phi += d * math.sin(2 * j * chi)
    lng0 = (zone - 1) * 6 - 180 + 3
    lam = math.atan2(math.sinh(eta_p), math.cos(xi_p))
    return math.degrees(phi), lng0 + math.degrees(lam)


def to_mgrs(lat: float, lng: float, digits: int = 10) -> str:
    """e.g. '43S CT 12345 67890' (digits = total digits, 2..10, even)."""
    band = band_for(lat)
    zone, _, e, n = to_utm(lat, lng)
    col = COL_SETS[(zone - 1) % 3][int(e // 100_000) - 1]
    row_offset = 5 if zone % 2 == 0 else 0
    row = ROW_LETTERS[(int(n // 100_000) + row_offset) % 20]
    half = digits // 2
    div = 10 ** (5 - half)
    ee = int((e % 100_000) // div)
    nn = int((n % 100_000) // div)
    return f"{zone}{band} {col}{row} {ee:0{half}d} {nn:0{half}d}"


_FULL = re.compile(r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])\s*([A-HJ-NP-V])\s*(\d[\d\s]*)?$", re.I)
_GZD = re.compile(r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*$", re.I)
_SQ = re.compile(r"^\s*([A-HJ-NP-Z])\s*([A-HJ-NP-V])\s*$", re.I)


def _digits(s: str) -> tuple[int, int, int]:
    d = re.sub(r"\s+", "", s or "")
    if not d.isdigit() or len(d) % 2 or not 2 <= len(d) <= 10:
        raise GridError("The numeric part must have an even number of digits (6 for a six-figure GR, up to 10)")
    half = len(d) // 2
    scale = 10 ** (5 - half)
    return int(d[:half]) * scale, int(d[half:]) * scale, scale


def from_mgrs(gzd: str, square: str, numeric: str) -> dict:
    """Convert grid zone + 100 km square + digits to lat/lng (SW corner)."""
    m, s = _GZD.match(gzd or ""), _SQ.match(square or "")
    if not m:
        raise GridError("Grid zone must look like 43S (zone number 1–60 and band letter)")
    if not s:
        raise GridError("100 km square must be two letters, e.g. CT")
    zone, band = int(m.group(1)), m.group(2).upper()
    if not 1 <= zone <= 60:
        raise GridError("Zone number must be 1–60")
    col_l, row_l = s.group(1).upper(), s.group(2).upper()
    cols = COL_SETS[(zone - 1) % 3]
    if col_l not in cols:
        raise GridError(f"Square column {col_l} does not exist in zone {zone} (valid: {cols})")
    e_off, n_off, precision = _digits(numeric)
    easting = (cols.index(col_l) + 1) * 100_000 + e_off
    row_offset = 5 if zone % 2 == 0 else 0
    n100k = ((ROW_LETTERS.index(row_l) - row_offset) % 20) * 100_000

    band_lat0 = -80 + BANDS.index(band) * 8
    band_lat1 = 84 if band == "X" else band_lat0 + 8
    northern = band_lat0 >= 0
    # The row letters repeat every 2,000 km: pick the repeat that falls in the band.
    for k in range(10):
        northing = n100k + n_off + k * 2_000_000
        lat, lng = from_utm(zone, northern, easting, northing)
        if band_lat0 - 0.5 <= lat <= band_lat1 + 0.5:
            break
    else:
        raise GridError(f"Square {col_l}{row_l} does not fall inside band {band} of zone {zone}")
    return {
        "lat": lat,
        "lng": lng,
        "precision_m": precision,
        "mgrs": f"{zone}{band} {col_l}{row_l} {''.join(numeric.split())}",
    }


def parse(text: str, default_gzd: str | None = None, default_square: str | None = None) -> dict:
    """Parse a full MGRS reference, or a bare GR using the default zone/square."""
    t = (text or "").strip().upper()
    m = _FULL.match(t)
    if m and m.group(5):
        return from_mgrs(m.group(1) + m.group(2), m.group(3) + m.group(4), m.group(5))
    if re.fullmatch(r"[\d\s]+", t):
        if not default_gzd or not default_square:
            raise GridError("Set the grid zone and 100 km square first (e.g. 43S and CT)")
        return from_mgrs(default_gzd, default_square, t)
    raise GridError("Enter a grid reference like 123456 or 43S CT 123456")
