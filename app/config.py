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

# Tracking thresholds, in metres.
ARRIVAL_RADIUS_M = float(os.environ.get("MDS_ARRIVAL_RADIUS_M", "25"))
OFF_ROUTE_M = float(os.environ.get("MDS_OFF_ROUTE_M", "50"))

# MLCU self-destruction range from the handout: 1-272 hours.
SD_MIN_HOURS = 1
SD_MAX_HOURS = 272
DEFAULT_SD_HOURS = 48
TOTAL_CANISTERS = 288


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, CROP_DIR, DOC_UPLOAD_DIR, REFERENCE_DIR):
        d.mkdir(parents=True, exist_ok=True)
