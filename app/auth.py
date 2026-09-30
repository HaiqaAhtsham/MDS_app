"""Password hashing, cookie sessions and the single-admin bootstrap."""

import hashlib
import hmac
import logging
import secrets
import time

from fastapi import Depends, HTTPException, Request

from . import config, db

log = logging.getLogger("mds.auth")
COOKIE = "mds_session"
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")


def bootstrap_admin() -> None:
    """Create the single admin account on first run."""
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            return
        password = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
        c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
            (config.ADMIN_USER, hash_password(password), time.time()),
        )
    if not config.ADMIN_PASSWORD:
        log.warning("Created admin user %r with generated password: %s", config.ADMIN_USER, password)
        log.warning("Change it after first login (Settings > Change password).")


def login(username: str, password: str) -> str | None:
    with db.tx() as c:
        user = c.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)",
            (token, user["id"], time.time() + config.SESSION_HOURS * 3600),
        )
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    return token


def logout(token: str) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not signed in")
    with db.tx() as c:
        row = c.execute(
            """SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ? AND u.active = 1""",
            (token, time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired")
    return dict(row)


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user
