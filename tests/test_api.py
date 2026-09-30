def test_requires_login(app_env):
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as c:
        assert c.get("/api/me").status_code == 401
        assert c.get("/api/markers").status_code == 401
        assert c.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401


def test_user_management(client):
    from fastapi.testclient import TestClient

    from app import main

    assert client.get("/api/me").json()["role"] == "admin"
    r = client.post("/api/users", json={"username": "op1", "password": "short"})
    assert r.status_code == 400
    r = client.post("/api/users", json={"username": "op1", "password": "operator-pass"})
    assert r.status_code == 200
    uid = r.json()["id"]
    assert client.post("/api/users", json={"username": "op1", "password": "operator-pass"}).status_code == 409

    with TestClient(main.app) as op:
        assert op.post("/api/login", json={"username": "op1", "password": "operator-pass"}).status_code == 200
        me = op.get("/api/me").json()
        assert me["role"] == "operator"
        assert op.get("/api/users").status_code == 403  # admin only
        assert op.get("/api/markers").status_code == 200

        # Disabling the user ends their session.
        client.patch(f"/api/users/{uid}", json={"active": False})
        assert op.get("/api/me").status_code == 401
        assert op.post("/api/login", json={"username": "op1", "password": "operator-pass"}).status_code == 401

    admin_id = client.get("/api/me").json()["id"]
    assert client.delete(f"/api/users/{admin_id}").status_code == 400  # single admin stays
    assert client.delete(f"/api/users/{uid}").status_code == 200


def test_change_password(client):
    assert client.post("/api/me/password", json={"current": "nope", "new": "new-password-1"}).status_code == 400
    assert client.post("/api/me/password", json={"current": "admin-pass-123", "new": "new-password-1"}).status_code == 200
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "admin", "password": "new-password-1"}).status_code == 200


def test_ammo(client):
    s = client.get("/api/settings").json()
    assert s["total_canisters"] == 288
    assert client.put("/api/settings/ammo", json={"fired": 33}).json()["fired_canisters"] == 33
    assert client.put("/api/settings/ammo", json={"fired": 999}).status_code == 400


def test_chat_answers_from_handout(client):
    r = client.post("/api/chat", json={"question": "How many first line salvos are authorised to MDS?"}).json()
    assert "6 x salvos" in r["answer"]
    assert any(s["page"] == 86 for s in r["sources"])

    r = client.post("/api/chat", json={"question": "What is the mine delivery range?"}).json()
    assert "40" in r["answer"] and "150" in r["answer"]

    r = client.post("/api/chat", json={"question": "How long does reloading of the launchers take?"}).json()
    assert "72" in r["answer"]
