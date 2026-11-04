import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def rag_cache(tmp_path_factory):
    """Extract the PDF text once per test run; it is the slow part."""
    from app import rag

    cache = {}
    for pdf in [ROOT / "MDS.pdf"]:
        if pdf.exists():
            cache[rag._fingerprint(pdf)] = rag.extract_pages(pdf)
    path = tmp_path_factory.mktemp("rag") / "rag_cache.json"
    path.write_text(json.dumps(cache))
    return path


@pytest.fixture()
def app_env(tmp_path, monkeypatch, rag_cache):
    """Fresh data dir, reference dir and admin password per test."""
    monkeypatch.setenv("MDS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MDS_REFERENCE_DIR", str(tmp_path / "reference"))
    monkeypatch.setenv("MDS_ADMIN_PASSWORD", "admin-pass-123")
    monkeypatch.setenv("MDS_RAG_MODE", "local")
    monkeypatch.setenv("MDS_ONLINE_ROUTING", "0")  # tests never call public servers
    from app import config

    importlib.reload(config)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rag_cache.json").write_text(rag_cache.read_text())
    yield tmp_path


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as c:
        r = c.post("/api/login", json={"username": "admin", "password": "admin-pass-123"})
        assert r.status_code == 200
        yield c
