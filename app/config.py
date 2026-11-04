"""Runtime configuration, read from environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MDS_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "mds.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
CROP_DIR = DATA_DIR / "crops"
DOC_UPLOAD_DIR = DATA_DIR / "docs"

# Reference pictures of the object to detect. Anything the admin uploads
# through the UI lands here too.
REFERENCE_DIR = Path(os.environ.get("MDS_REFERENCE_DIR", ROOT / "reference"))

# Optional YOLO weights. When present (and `ultralytics` is installed) they
# replace the built-in template matcher.
YOLO_WEIGHTS = Path(os.environ.get("MDS_YOLO_WEIGHTS", ROOT / "models" / "detector.pt"))

# Documents indexed by the RAG chatbot at startup.
BUILTIN_DOCS = [ROOT / "MDS.pdf", *sorted((ROOT / "docs").glob("*.pdf"))]

ADMIN_USER = os.environ.get("MDS_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("MDS_ADMIN_PASSWORD")  # generated if unset
SESSION_HOURS = int(os.environ.get("MDS_SESSION_HOURS", "12"))
COOKIE_SECURE = os.environ.get("MDS_COOKIE_SECURE", "0") == "1"

# "local" keeps the document on this server (BM25 retrieval + extractive
# answers). "claude" sends the retrieved excerpts to the Claude API to
# generate the answer.
RAG_MODE = os.environ.get("MDS_RAG_MODE", "local").lower()
CLAUDE_MODEL = os.environ.get("MDS_CLAUDE_MODEL", "claude-opus-5-5")

# Road routing: a self-hosted OSRM server, e.g. http://localhost:5000.
OSRM_URL = os.environ.get("MDS_OSRM_URL", "")
OSRM_PROFILE = os.environ.get("MDS_OSRM_PROFILE", "driving")
# Optional second self-hosted OSRM built with a profile that allows tracks
# (unpaved highway=track / path), for "Roads and tracks" routing.
OSRM_TRACKS_URL = os.environ.get("MDS_OSRM_TRACKS_URL", "")
# Without a self-hosted server (or when it is down), use the public OSRM
# servers run for OpenStreetMap. They need internet; 0 turns them off.
ONLINE_ROUTING = os.environ.get("MDS_ONLINE_ROUTING", "1") == "1"
PUBLIC_OSRM = {
    "roads": "https://routing.openstreetmap.de/routed-car",
    # The car profile skips unpaved tracks; the bike profile follows them.
    "tracks": "https://routing.openstreetmap.de/routed-bike",
}
# Travel time on a roads-and-tracks route is distance / this speed (the
# bike profile's own times are for a bicycle).
TRACK_SPEED_KMH = float(os.environ.get("MDS_TRACK_SPEED_KMH", "25"))

# Offline map tiles: an .mbtiles file, or a folder of {z}/{x}/{y}.png|jpg.
# When neither exists, the browser loads online imagery (if MDS_ONLINE_TILES=1).
MBTILES = Path(os.environ.get("MDS_MBTILES", ROOT / "tiles" / "map.mbtiles"))
TILE_DIR = Path(os.environ.get("MDS_TILE_DIR", ROOT / "tiles"))
ONLINE_TILES = os.environ.get("MDS_ONLINE_TILES", "1") == "1"
ONLINE_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
# Says, per tile, whether Esri has imagery there, so the map can stop at the
# deepest zoom that exists instead of showing "Map data not yet available".
ONLINE_TILEMAP_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tilemap"
ONLINE_TILE_MAX_ZOOM = 19

# External GPS receiver on a serial port, e.g. COM5 (Windows) or
# /dev/ttyUSB0 (Linux). When set, the server connects to it at startup.
# MDS_GPS_BAUD: 4800, 9600, ... or empty to detect it.
GPS_PORT = os.environ.get("MDS_GPS_PORT", "")
GPS_BAUD = int(os.environ["MDS_GPS_BAUD"]) if os.environ.get("MDS_GPS_BAUD") else None
GPS_INTERVAL_S = float(os.environ.get("MDS_GPS_INTERVAL_S", "1"))

# Tracking thresholds, in metres.
ARRIVAL_RADIUS_M = float(os.environ.get("MDS_ARRIVAL_RADIUS_M", "25"))
OFF_ROUTE_M = float(os.environ.get("MDS_OFF_ROUTE_M", "50"))

# Self-destruct time for marks. The handout gives 1-272 hours (MLCU) and
# 4-56 minutes (portable setter); custom values down to 1 minute are allowed.
SD_MIN_HOURS = 1 / 60
SD_MAX_HOURS = 272
DEFAULT_SD_HOURS = 48
CANISTERS_PER_SALVO = 288  # 8 scattering boxes x 36 (handout p. 8)


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, CROP_DIR, DOC_UPLOAD_DIR, REFERENCE_DIR):
        d.mkdir(parents=True, exist_ok=True)
