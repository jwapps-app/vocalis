"""Passwords, session tokens, and the credential the narrator presents.

Single user, so there is no accounts table — but the shape follows Scrinium's:
bcrypt for the password, a signed bearer token for the session, and a FastAPI
dependency the routes declare.

Two kinds of caller, and they cannot share a mechanism:

* a browser, which can show a login form and hold a token;
* the narrator, which fetches books with curl on another machine and has no
  way to fill anything in. It carries a token issued by this server and shipped
  inside the worker bundle — which is itself behind the password, so obtaining
  one means already being logged in.
"""

import hmac
import os
import secrets
import time
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import pool

ALGORITHM = "HS256"
# Long-lived on purpose: this is a personal tool on a home network, and being
# signed out mid-book to no benefit is a worse outcome than a long session.
SESSION_DAYS = 30

bearer_scheme = HTTPBearer(auto_error=False)


def _instance() -> dict:
    """The single settings row, with its secrets generated on first use."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT password_hash, secret_key, worker_token FROM instance WHERE id"
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                INSERT INTO instance (id, secret_key, worker_token)
                VALUES (true, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING password_hash, secret_key, worker_token
                """,
                (secrets.token_hex(32), secrets.token_hex(32)),
            ).fetchone()
            if row is None:  # another worker inserted it first
                row = conn.execute(
                    "SELECT password_hash, secret_key, worker_token FROM instance WHERE id"
                ).fetchone()
    return row


def is_configured() -> bool:
    """Whether a password has been set. Until it is, the UI asks for one."""
    return bool(_instance()["password_hash"])


def set_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with pool.connection() as conn:
        conn.execute("UPDATE instance SET password_hash = %s WHERE id", (digest,))


def verify_password(password: str) -> bool:
    stored = _instance()["password_hash"]
    if not stored:
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        return False


def mint_session() -> str:
    payload = {"sub": "owner", "exp": int(time.time()) + SESSION_DAYS * 86400}
    return jwt.encode(payload, _instance()["secret_key"], algorithm=ALGORITHM)


def worker_token() -> str:
    return _instance()["worker_token"]


def session_valid(token: str) -> bool:
    try:
        jwt.decode(token, _instance()["secret_key"], algorithms=[ALGORITHM])
        return True
    except jwt.PyJWTError:
        return False


def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    x_vocalis_worker: Annotated[str | None, Header()] = None,
) -> None:
    """Accept either a signed browser session or the narrator's token.

    Until a password is set the instance is open — otherwise first-run setup
    would be impossible. `is_configured()` is what the UI uses to force that
    step immediately, so the window is the seconds between `docker compose up`
    and choosing a password, not a standing invitation.
    """
    if not is_configured():
        return
    if x_vocalis_worker and hmac.compare_digest(x_vocalis_worker, worker_token()):
        return
    if credentials and session_valid(credentials.credentials):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


Authenticated = Annotated[None, Depends(require_auth)]
