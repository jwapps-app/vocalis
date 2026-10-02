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
import threading
import time
from datetime import datetime, timedelta, timezone
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


_COLUMNS = ("username, password_hash, secret_key, worker_token,"
            " session_epoch, enrol_code, enrol_expires,"
            " enrol_prev_code, enrol_prev_expires")

# The settings row, kept in memory between requests.
#
# It was being read twice on every single request — once to ask whether a
# password exists, once to get the key a session is signed with — so the
# cheapest endpoint in the app cost three round trips to serve one row of
# actual data, on a pool of five connections, against a UI that polls. The row
# changes when credentials are set or a session is revoked, and at no other
# time.
#
# Writes here clear it directly; the short expiry is only so a second API
# process, which cannot be told, converges quickly.
_CACHE_SECONDS = 5.0
_cached: dict | None = None
_cached_at = 0.0
_lock = threading.Lock()


def _forget() -> None:
    """Drop the cached row after a write."""
    global _cached
    with _lock:
        _cached = None


def _load() -> dict:
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
            if row is None:  # another process inserted it first
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM instance WHERE id"
                ).fetchone()
    return row


def _instance() -> dict:
    """The single settings row, with its secrets generated on first use."""
    global _cached, _cached_at
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached_at < _CACHE_SECONDS:
            return _cached
    row = _load()
    with _lock:
        _cached, _cached_at = row, time.monotonic()
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
    _forget()


def set_username(username: str) -> None:
    """Name an instance that predates usernames, leaving its password alone."""
    username = _check_username(username)
    with pool.connection() as conn:
        conn.execute("UPDATE instance SET username = %s WHERE id", (username,))
    _forget()


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


def _epoch() -> int:
    return _instance()["session_epoch"] or 1


def mint_session() -> str:
    """A session, stamped with the generation it belongs to."""
    payload = {
        "sub": "owner",
        "gen": _epoch(),
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }
    return jwt.encode(payload, _instance()["secret_key"], algorithm=ALGORITHM)


def session_valid(token: str) -> bool:
    """Whether a session is signed, unexpired, and not signed out.

    The generation is what makes signing out mean something. A JWT is valid
    until it expires and cannot be withdrawn, so deleting the cookie only asked
    the browser to forget it — anyone who had already copied it stayed signed
    in for the rest of the month. Bumping the generation invalidates every
    token issued before it, which is the behaviour "Sign out" claims.
    """
    try:
        claims = jwt.decode(token, _instance()["secret_key"], algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return False
    return claims.get("gen") == _epoch()


def revoke_sessions() -> None:
    """Sign out. One account, so this ends every session there is."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE instance SET session_epoch = COALESCE(session_epoch, 1) + 1"
            " WHERE id"
        )
    _forget()


def worker_token() -> str:
    return _instance()["worker_token"]


# --- enrolling a narrator -------------------------------------------------
#
# The install command is fetched by `curl … | sh`, which has no session and no
# way to be given a header, so its credential has to sit in the URL — where it
# is written to the access log of every proxy between here and the caller, and
# into shell history.
#
# That is survivable for a code which only fetches the installer and expires,
# and was not for the worker token: the same value authenticated every endpoint
# in the API, so a line in a log file was enough to delete the whole library.
# They are now different secrets with different reach.
ENROLMENT_MINUTES = 30

# A code is handed out unchanged until it has less than this left to live, so
# the command on the Setup page is stable while someone copies and runs it.
_ROTATE_WITHIN_MINUTES = 10


def current_enrolment() -> str:
    """The code to put in the install command — the same one, for a while.

    This used to mint a fresh code on every call. The call is /api/worker,
    which the app polls every five seconds — twice over with the Setup page
    open — so the command you copied was replaced within seconds and failed
    with a 401 when you ran it. Each mint also emptied the settings cache,
    which put the database round trips the cache exists to avoid back on
    every request.

    Now a code is reused until it has under ten minutes left, then rotated, and
    the one it replaces stays valid until its own expiry. Any code that has been
    on screen is therefore good for at least ten more minutes.

    Rotation is one conditional UPDATE, so two polls arriving together cannot
    both rotate and strand a code one of them just displayed. It only runs near
    expiry: the rest of the time the cached settings row already holds a code
    with enough life left, and a poll costs no query at all.
    """
    row = _instance()
    if (row["enrol_code"] and row["enrol_expires"]
            and row["enrol_expires"] - datetime.now(timezone.utc)
            > timedelta(minutes=_ROTATE_WITHIN_MINUTES)):
        return row["enrol_code"]

    with pool.connection() as conn:
        rotated = conn.execute(
            """
            UPDATE instance SET
                enrol_prev_code = enrol_code,
                enrol_prev_expires = enrol_expires,
                enrol_code = %s,
                enrol_expires = now() + %s * interval '1 minute'
            WHERE id AND (enrol_code IS NULL
                          OR enrol_expires < now() + %s * interval '1 minute')
            RETURNING enrol_code
            """,
            (secrets.token_hex(16), ENROLMENT_MINUTES, _ROTATE_WITHIN_MINUTES),
        ).fetchone()
        if rotated:
            code = rotated["enrol_code"]
        else:
            code = conn.execute(
                "SELECT enrol_code FROM instance WHERE id"
            ).fetchone()["enrol_code"]
    if rotated:
        _forget()   # only when something changed — not on every poll
    return code


def enrolment_valid(code: str) -> bool:
    """Whether `code` is the current enrolment code or the one before it,
    and has not expired. Both are compared, whichever matches, so the time
    taken does not reveal which slot a guess was closest to."""
    row = _instance()
    now = datetime.now(timezone.utc)
    ok = False
    for stored, expires in ((row["enrol_code"], row["enrol_expires"]),
                            (row["enrol_prev_code"], row["enrol_prev_expires"])):
        if stored and expires and expires > now:
            ok = hmac.compare_digest(code, stored) or ok
    return ok


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
