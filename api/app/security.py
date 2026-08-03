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


_COLUMNS = "username, password_hash, secret_key, worker_token"


def _instance() -> dict:
    """The single settings row, with its secrets generated on first use."""
    with pool.connection() as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM instance WHERE id").fetchone()
        if row is None:
            row = conn.execute(
                f"""
                INSERT INTO instance (id, secret_key, worker_token)
                VALUES (true, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (secrets.token_hex(32), secrets.token_hex(32)),
            ).fetchone()
            if row is None:  # another worker inserted it first
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM instance WHERE id"
                ).fetchone()
    return row


def is_configured() -> bool:
    """Whether a password has been set. Until it is, the UI asks for one."""
    return bool(_instance()["password_hash"])


def has_username() -> bool:
    """Whether a username has been chosen.

    False on an instance set up before usernames existed: it has a password and
    works, but signs in on the password alone. The UI uses this to ask for a
    username once, rather than inventing a default — a migration that quietly
    named everyone `admin` would hand an attacker half the credential on every
    Vocalis on the internet.
    """
    return bool(_instance()["username"])


def _check_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Use at least 8 characters.")


def _check_username(username: str) -> str:
    username = username.strip()
    if not 3 <= len(username) <= 64:
        raise HTTPException(400, "Use a username of 3 to 64 characters.")
    return username


def set_credentials(username: str, password: str) -> None:
    username = _check_username(username)
    _check_password(password)
    digest = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE instance SET username = %s, password_hash = %s WHERE id",
            (username, digest),
        )


def set_username(username: str) -> None:
    """Name an instance that predates usernames, leaving its password alone."""
    username = _check_username(username)
    with pool.connection() as conn:
        conn.execute("UPDATE instance SET username = %s WHERE id", (username,))


def verify_credentials(username: str, password: str) -> bool:
    """Check a sign-in.

    The password is hashed whether or not the username matched, and the two
    results are only combined at the end. Returning early on a bad username
    would answer in the microseconds bcrypt deliberately does not, which tells
    an attacker when they have guessed the name — and the point of having a
    username at all is that it is the half they do not know.
    """
    row = _instance()
    stored_hash, stored_name = row["password_hash"], row["username"]
    if not stored_hash:
        return False
    try:
        password_ok = bcrypt.checkpw(password.encode(), stored_hash.encode())
    except ValueError:
        return False
    if not stored_name:
        return password_ok  # set up before usernames; the password is the whole key
    # Case-insensitive: a name is not a secret worth failing on capitalisation.
    name_ok = hmac.compare_digest(
        username.strip().casefold().encode(), stored_name.casefold().encode()
    )
    return name_ok and password_ok


# --- brute force ---------------------------------------------------------
#
# Counted for the instance as a whole rather than per client address. Vocalis
# has one user, and behind a reverse proxy every request arrives from the
# proxy — so a per-address counter would either lump the internet together
# anyway or have to trust a forwarded header the caller can set at will, which
# an attacker resets by varying it.
#
# The delay is a pause, never a lock, and it decays: a quiet spell clears the
# count, so a burst of wrong guesses today does not still be costing the owner
# thirty seconds an hour later.
#
# The honest trade: because the count is shared, someone hammering the login
# can keep the owner waiting up to _MAX_DELAY. That is the price of not
# trusting a forwarded address, and it is bounded — thirty seconds, not a
# lockout, and only while the attack is actually running. Blocking by address
# instead would look kinder and stop nothing, since the address is the
# attacker's to choose.
_MAX_DELAY = 30.0
_FREE_ATTEMPTS = 5
_FORGET_AFTER = 900.0          # 15 minutes of quiet and the slate is clean
_failures = 0
_retry_at = 0.0
_last_failure = 0.0


def _decay() -> None:
    global _failures
    if _failures and time.monotonic() - _last_failure > _FORGET_AFTER:
        _failures = 0


def login_wait() -> float:
    """Seconds the caller must wait before another attempt is considered."""
    _decay()
    return max(0.0, _retry_at - time.monotonic())


def note_login_failure() -> None:
    global _failures, _retry_at, _last_failure
    _decay()
    _failures += 1
    _last_failure = time.monotonic()
    if _failures > _FREE_ATTEMPTS:
        backoff = min(_MAX_DELAY, 2.0 ** (_failures - _FREE_ATTEMPTS))
        _retry_at = _last_failure + backoff


def note_login_success() -> None:
    global _failures, _retry_at
    _failures, _retry_at = 0, 0.0


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
