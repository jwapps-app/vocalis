import hmac
import io
import json
import os
import shutil
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (Body, FastAPI, File, Form, HTTPException, Request,
                     UploadFile, status)
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)

from bs4 import BeautifulSoup

from vocalis_core.epub_parse import parse_epub
from vocalis_core.text_clean import chunk_paragraphs, clean_text, find_citations

from .db import pool
from .narrators import list_narrators, resolve
from .security import (
    SESSION_DAYS,
    has_username,
    is_configured,
    login_wait,
    mint_session,
    note_login_failure,
    note_login_success,
    session_valid,
    set_credentials,
    set_username,
    verify_credentials,
    worker_token,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# Narrator voices baked into the image, copied into the data directory the
# first time the stack runs.
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", "/srv/assets"))


def seed_data_dir() -> None:
    """Populate an empty data directory from the assets in the image.

    A new deployment should work from compose and .env alone. Without this the
    narrator dropdown is empty until someone hand-copies ten reference clips
    from another machine — the one step that could not be automated away, and
    the one most likely to be missed.

    Copies file by file and never overwrites, so narrators added later with
    add_narrator.py, and the manifest listing them, survive every restart.
    """
    source = ASSETS_DIR / "narrators"
    if not source.is_dir():
        return
    seeded = 0
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        target = DATA_DIR / "narrators" / path.relative_to(source)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        seeded += 1
    if seeded:
        print(f"Seeded {seeded} narrator file(s) into {DATA_DIR / 'narrators'}", flush=True)


# Schema added after the first release. db/init.sql only runs against an empty
# volume, so an existing installation upgrading by `docker compose pull` would
# otherwise keep a database with no `instance` table — and every request would
# fail inside the auth check, which is a worse failure than the feature simply
# being absent. Written to be safe to run on every start.
MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS instance (
      id            BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
      password_hash TEXT,
      secret_key    TEXT,
      worker_token  TEXT,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS drop_citations BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS work_seconds REAL NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS audio_seconds REAL",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS free_gpu_gb REAL",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS timings JSONB",
    "ALTER TABLE workers ADD COLUMN IF NOT EXISTS revision INT",
    # Nullable on purpose: an instance set up before usernames existed keeps
    # working on its password, and is asked to choose a name once.
    "ALTER TABLE instance ADD COLUMN IF NOT EXISTS username TEXT",
]


def migrate() -> None:
    with pool.connection() as conn:
        for statement in MIGRATIONS:
            try:
                conn.execute(statement)
            except Exception as exc:   # noqa: BLE001 - one bad step must not stop the rest
                print(f"migration skipped ({exc})", flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate()
    seed_data_dir()
    yield


app = FastAPI(title="Vocalis API", lifespan=lifespan)

# Reachable without a session. Everything else is closed.
PUBLIC_PATHS = {
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Require a session on every route except the handful named above.

    A middleware rather than a dependency on each route: with two dozen
    endpoints, the failure mode of the per-route approach is that a new one
    silently ships unprotected, and nothing about it looks wrong in review.
    Here the default is closed and exposing something takes a deliberate edit
    to PUBLIC_PATHS.
    """
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_PATHS:
        return await call_next(request)

    # Open until a password exists, so the first visit can set one.
    if not is_configured():
        return await call_next(request)

    # Header for the running narrator; query parameter for enrolling one.
    # `curl … | sh` cannot hold a session and cannot be given a header by the
    # person pasting it, so the command shown on the setup page — a page only
    # reachable once logged in — carries the key in its URL instead.
    presented = request.headers.get("x-vocalis-worker") or request.query_params.get("key")
    if presented and hmac.compare_digest(presented, worker_token()):
        return await call_next(request)

    session = request.cookies.get(SESSION_COOKIE)
    if session and session_valid(session):
        return await call_next(request)
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)

JOB_COLUMNS = """
    id, status, epub_filename, title, author, seed, narrator, mode, chapters,
    concurrency, cancel_requested,
    chapter_count, chapters_done, progress, estimated_total_seconds,
    work_seconds, audio_seconds,
    -- The blob itself is thousands of chunks — hundreds of kilobytes for a long
    -- book — and the list is polled every couple of seconds. All the list needs
    -- is whether it exists; the chapter marks have their own endpoint.
    (timings IS NOT NULL) AS has_timings,
    error, created_at, updated_at, started_at, finished_at,
    (output_path IS NOT NULL) AS has_output
"""

# Ceiling on the concurrency picker. The worker clamps this further to what its
# own machine's memory allows and reports that back via its heartbeat.
MAX_CONCURRENCY = 6

# Statuses where the worker owns the job and files must not be touched.
RUNNING = ("parsing", "synthesizing", "assembling")

# The worker bundle source, copied into the image at build time.
BUNDLE_ROOT = Path(os.environ.get("BUNDLE_ROOT", "/srv/bundle"))

# Where the GPU machine reaches Postgres. On the same Mac as the server this is
# the host-published port (127.0.0.1:5445); for a worker on another machine,
# set WORKER_DB_HOSTPORT to the server's LAN address and open the port.
WORKER_DB_HOSTPORT = os.environ.get("WORKER_DB_HOSTPORT", "127.0.0.1:5445")

# The narrator revision this server expects — see identity.REVISION in the
# worker. A narrator below it is out of date, and the UI says so rather than
# letting features go missing without explanation.
REQUIRED_WORKER_REVISION = 3

# A worker whose heartbeat is older than this reads as offline. Comfortably
# above the worker's poll interval so a busy narrator is never called dead.
WORKER_STALE_SECONDS = 30

# The port the web UI is published on, so the bundle can tell the worker where
# to fetch books from. Matches WEB_PORT in the compose file.
WEB_PORT = os.environ.get("WEB_PORT", "8091")


# ------------------------------------------------------------------- auth
#
# Left open until a password exists, so the first visit can set one — the same
# first-run flow Portainer uses. Nothing here requires editing a compose file
# or fishing a generated password out of container logs.


@app.get("/api/auth/status")
def auth_status():
    return {"configured": is_configured(), "username_set": has_username()}


SESSION_COOKIE = "vocalis_session"


def _issue_session(request: Request) -> JSONResponse:
    """Hand back the session as a cookie rather than a token for the page to
    hold.

    Covers, voice previews and the finished audiobook are fetched by the
    browser itself — <img src>, <audio>, a download link — and none of those
    can carry an Authorization header. A cookie is sent on all of them without
    the page being involved. SameSite=Lax keeps it off cross-site requests,
    which is what a bearer token was buying.
    """
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        mint_session(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        # Only over TLS, where there is TLS. Setting it unconditionally would
        # make the cookie silently unusable on a plain-HTTP LAN install.
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        path="/",
    )
    return response


@app.post("/api/auth/setup", status_code=201)
def auth_setup(
    request: Request,
    username: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
):
    """Choose the credentials, once. Refuses to overwrite existing ones, or
    anyone who can reach the port could take the instance over."""
    if is_configured():
        raise HTTPException(409, "A password is already set")
    set_credentials(username, password)
    return _issue_session(request)


@app.post("/api/auth/login")
def auth_login(
    request: Request,
    password: str = Body(..., embed=True),
    username: str = Body("", embed=True),
):
    """Sign in.

    One message for every kind of failure. Saying "no such user" would confirm
    a guessed name for free, and the username is half of what an exposed
    instance is protected by.
    """
    wait = login_wait()
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    if not verify_credentials(username, password):
        note_login_failure()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Wrong username or password"
        )
    note_login_success()
    return _issue_session(request)


@app.post("/api/auth/username")
def auth_set_username(username: str = Body(..., embed=True)):
    """Name an instance that was set up before usernames existed.

    Behind the session check like everything else, so it is the person already
    signed in who chooses — and once set, the ordinary login path applies.
    """
    if has_username():
        raise HTTPException(409, "A username is already set")
    set_username(username)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- narrators


@app.get("/api/narrators")
def get_narrators():
    previews = DATA_DIR / "narrators" / "previews"
    return [
        {
            "id": n["id"],
            "name": n["name"],
            "description": n["description"],
            "has_preview": (previews / f"{n['id']}.wav").is_file(),
        }
        for n in list_narrators()
    ]


@app.get("/api/narrators/{narrator_id}/clip")
def narrator_clip(narrator_id: str):
    """The reference recording a narrator is cloned from.

    Served rather than shared: the worker used to read this straight off a
    mounted copy of the data directory, which is what forced every split
    installation to set up a network share.
    """
    preset = resolve(narrator_id)
    if preset is None:
        raise HTTPException(404, "unknown narrator")
    if not preset["ref"]:
        raise HTTPException(404, "this narrator has no reference clip")
    path = DATA_DIR / preset["ref"]
    if not path.is_file():
        raise HTTPException(404, "reference clip missing from the data dir")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/narrators/{narrator_id}/preview")
def narrator_preview(narrator_id: str):
    if resolve(narrator_id) is None:
        raise HTTPException(404, "unknown narrator")
    path = DATA_DIR / "narrators" / "previews" / f"{narrator_id}.wav"
    if not path.is_file():
        raise HTTPException(404, "no preview rendered for this narrator")
    return FileResponse(path, media_type="audio/wav")


# ---------------------------------------------------------------- jobs


def _save_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


def _own_upload(job_id, rel: str) -> str:
    """Confirm a client-supplied path is one this API minted for this job.

    `voice_ref_path` arrives in a request body and is later joined to the data
    directory by the worker, which runs natively outside the container. Path's
    `/` discards the left side when the right is absolute — `DATA_DIR /
    "/etc/passwd"` is `/etc/passwd` — so an unchecked value is an arbitrary file
    read on the host, not merely an escape from the data directory. Resolve it
    and require that it land directly in this job's own upload folder.
    """
    root = (DATA_DIR / "uploads" / str(job_id)).resolve()
    try:
        candidate = (DATA_DIR / rel).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "invalid voice reference")
    if candidate.parent != root or not candidate.is_file():
        raise HTTPException(400, "invalid voice reference")
    return str(candidate.relative_to(DATA_DIR.resolve()))


# Covers are written to disk with the extension the EPUB gave them and later
# served back; an EPUB naming its cover ".html" or ".svg" would otherwise choose
# the Content-Type of a same-origin response whose bytes it also controls.
COVER_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}

# An EPUB is a zip, and 200 MB of upload can hide far more than that once
# expanded. ebooklib reads items into memory, so the ceiling is the API
# container's RAM.
MAX_EPUB_UNPACKED = 400 * 1024 * 1024


def _reject_zip_bomb(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile:
        raise HTTPException(400, "that file is not a valid EPUB")
    if total > MAX_EPUB_UNPACKED:
        raise HTTPException(400, "this EPUB expands to more than 400 MB")


def _job_dirs(job_id) -> list[Path]:
    """Every directory Vocalis owns for a job. job_id is a UUID, so these
    paths cannot escape DATA_DIR."""
    return [DATA_DIR / part / str(job_id) for part in ("uploads", "work", "output")]


def _drop_samples(conn, parent_id) -> None:
    """Delete a draft's audition samples and their audio."""
    rows = conn.execute(
        "DELETE FROM jobs WHERE parent_id = %s AND mode = 'sample' RETURNING id",
        (parent_id,),
    ).fetchall()
    for row in rows:
        for directory in _job_dirs(row["id"]):
            shutil.rmtree(directory, ignore_errors=True)


def _fetch(conn, job_id):
    row = conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    return row


@app.post("/api/jobs/analyze", status_code=201)
def analyze(epub: UploadFile = File(...)):
    """Upload and parse an EPUB, returning a reviewable chapter plan.

    Nothing is synthesized yet — the job sits in 'draft' until /start.
    """
    if not (epub.filename or "").lower().endswith(".epub"):
        raise HTTPException(400, "expected an .epub file")

    job_id = uuid.uuid4()
    epub_rel = f"uploads/{job_id}/book.epub"
    _save_upload(epub, DATA_DIR / epub_rel)
    _reject_zip_bomb(DATA_DIR / epub_rel)

    try:
        book = parse_epub(DATA_DIR / epub_rel)
    except Exception as exc:
        raise HTTPException(400, f"could not read EPUB: {exc}")
    if not book.chapters:
        raise HTTPException(400, "no readable chapters found in this EPUB")

    if book.cover:
        # Fall back to .jpg rather than trusting the EPUB's own extension.
        ext = book.cover_ext.lower() if book.cover_ext.lower() in COVER_TYPES else ".jpg"
        (DATA_DIR / f"uploads/{job_id}/cover{ext}").write_bytes(book.cover)

    plan = [
        {
            "index": i,
            "title": ch.title,
            "source": ch.source,
            "include": not ch.front_matter,
            "chars": ch.chars,
        }
        for i, ch in enumerate(book.chapters)
    ]

    with pool.connection() as conn:
        row = conn.execute(
            f"""
            INSERT INTO jobs (id, status, epub_filename, epub_path, title, author,
                              chapter_count, chapters)
            VALUES (%s, 'draft', %s, %s, %s, %s, %s, %s)
            RETURNING {JOB_COLUMNS}
            """,
            (job_id, epub.filename, epub_rel, book.title, book.author,
             len(book.chapters), json.dumps(plan)),
        ).fetchone()
    return row


# Examples returned for the review screen to page through. The per-chapter
# counts are always exact; only the illustrative list is bounded, so a book with
# thousands of references still reports an honest number.
MAX_CITATION_SAMPLES = 300


@app.get("/api/jobs/{job_id}/citations")
def job_citations(job_id: uuid.UUID):
    """What "Skip inline references" would remove from this book.

    Counts are per chapter so the review screen can total only the chapters
    currently ticked, and stay right as that selection changes.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT epub_path FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")

    try:
        book = parse_epub(DATA_DIR / row["epub_path"])
    except Exception as exc:
        raise HTTPException(400, f"could not read EPUB: {exc}")

    counts: dict[int, int] = {}
    items: list[dict] = []
    for index, chapter in enumerate(book.chapters):
        found = find_citations(chapter.text)
        if not found:
            continue
        counts[index] = len(found)
        for item in found:
            if len(items) < MAX_CITATION_SAMPLES:
                items.append({**item, "chapter": index, "title": chapter.title})

    return {
        "counts": counts,
        "items": items,
        "truncated": sum(counts.values()) > len(items),
    }


# Enough of a chapter to tell an editor's note from the copyright page, without
# shipping a whole book to the review screen.
EXCERPT_CHARS = 700

# Must match the worker's config, or re-chunking here would not reproduce
# what was narrated and every chapter would fail its alignment check.
MAX_CHUNK_CHARS = int(os.environ.get("VOCALIS_MAX_CHUNK_CHARS", "300"))


@app.get("/api/jobs/{job_id}/excerpts")
def job_excerpts(job_id: uuid.UUID, drop_citations: bool = False):
    """The opening words of every section, for deciding what to include.

    A title alone does not say whether a section is the author's preface or the
    publisher's copyright block — and titles guessed from the text are exactly
    the ones worth checking. Every chapter is returned in one response so the
    review screen can expand any of them without re-parsing the EPUB.

    The text is cleaned the same way the narrator will clean it, so what is
    shown is what will be read aloud.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT epub_path FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")

    try:
        book = parse_epub(DATA_DIR / row["epub_path"])
    except Exception as exc:
        raise HTTPException(400, f"could not read EPUB: {exc}")

    excerpts = []
    for index, chapter in enumerate(book.chapters):
        spoken = clean_text(chapter.text, drop_citations=drop_citations)
        excerpts.append(
            {
                "index": index,
                "chars": len(spoken),
                "excerpt": spoken[:EXCERPT_CHARS],
                "truncated": len(spoken) > EXCERPT_CHARS,
            }
        )
    return {"excerpts": excerpts}


@app.get("/api/jobs/{job_id}/recorded")
def job_recorded(job_id: uuid.UUID):
    """Which chapter indexes already have audio on disk.

    Editing a finished book is only cheap for chapters that were narrated:
    dropping one is a repackage, but re-adding a section that was skipped the
    first time means recording it. The review screen uses this to say which is
    which instead of letting a tick-box quietly start hours of work.

    Derived from the job's own plan rather than from files, because the audio
    now lives on the worker's local disk and this server never sees it. For a
    finished book the two agree exactly: every included chapter was narrated,
    and every excluded one was not.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, chapters FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] != "done" or not row["chapters"]:
        return {"indexes": []}
    return {
        "indexes": sorted(
            c["index"] for c in row["chapters"] if c.get("include")
        )
    }


# ------------------------------------------------- files the worker exchanges
#
# The worker runs on whichever machine has the GPU, which is routinely not this
# one. It fetches what it needs and posts back what it produced, so the two
# halves share a database and nothing else — no mounted volume, no matching
# paths, no credentials for a file share.


@app.get("/api/jobs/{job_id}/epub")
def job_epub(job_id: uuid.UUID):
    """The uploaded book, for the worker to parse."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT epub_path FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    path = DATA_DIR / row["epub_path"]
    if not path.is_file():
        raise HTTPException(404, "the uploaded EPUB is missing")
    return FileResponse(path, media_type="application/epub+zip")


@app.get("/api/jobs/{job_id}/voice")
def job_voice(job_id: uuid.UUID):
    """A custom reference clip uploaded for this job, if there is one."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT voice_ref_path FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if not row["voice_ref_path"]:
        raise HTTPException(404, "this job uses a preset narrator")
    path = DATA_DIR / row["voice_ref_path"]
    if not path.is_file():
        raise HTTPException(404, "reference clip missing")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/jobs/{job_id}/output", status_code=201)
def upload_output(job_id: uuid.UUID, file: UploadFile = File(...)):
    """Receive a finished audiobook (or audition) from the worker.

    The worker sets the job's own status; this only stores the file and records
    where it landed, so a failed upload cannot leave a job marked done with
    nothing to download.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT mode FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")

        name = "sample.wav" if row["mode"] == "sample" else "book.m4b"
        rel = f"output/{job_id}/{name}"
        _save_upload(file, DATA_DIR / rel)
        conn.execute("UPDATE jobs SET output_path = %s WHERE id = %s", (rel, job_id))
    return {"output_path": rel}


@app.get("/api/jobs/{job_id}/chapters")
def job_chapters(job_id: uuid.UUID):
    """Where each chapter begins in the finished recording.

    Its own endpoint, and its own slice of the JSONB, because this is the small
    half of the timing data — a few dozen marks against thousands of chunks —
    and the player wants only this. Asking Postgres for `timings -> 'chapters'`
    keeps the rest of the blob out of the response entirely.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT timings -> 'chapters' AS marks FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    return {"chapters": row["marks"] or []}


@app.get("/api/jobs/{job_id}/read")
def job_read(job_id: uuid.UUID):
    """The book as it was printed, joined to when each part is spoken.

    Two halves that only mean something together: the blocks carry the markup
    the parser used to discard, and the timings recorded during synthesis say
    when each chunk of text is read aloud. Chunks never cross a paragraph
    break, so every one belongs inside a single block and the join needs no
    character offsets.

    Alignment is verified per chapter rather than assumed. Re-chunking here has
    to reproduce exactly what the narrator chunked; if the counts disagree —
    a book narrated before a chunking change, say — that chapter is returned
    with its text and no timings, so it reads correctly and simply does not
    follow along. Highlighting the wrong sentence would be worse than
    highlighting none.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT epub_path, chapters, timings, drop_citations, status"
            " FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if not row["timings"]:
        raise HTTPException(
            409,
            "This book was narrated before timings were recorded, so it cannot "
            "follow along. Converting it again would add them.",
        )

    try:
        book = parse_epub(DATA_DIR / row["epub_path"])
    except Exception as exc:
        raise HTTPException(400, f"could not read EPUB: {exc}")

    plan = {c["index"]: c for c in (row["chapters"] or [])}
    drop = bool(row["drop_citations"])

    # Timed chunks, grouped by the chapter they belong to.
    by_chapter: dict[int, list] = {}
    for chunk in row["timings"].get("chunks", []):
        by_chapter.setdefault(chunk["chapter"], []).append(chunk)
    chapter_times = {c["index"]: c for c in row["timings"].get("chapters", [])}

    # The narrator numbers chapters by position among those it narrated, not by
    # position in the EPUB, so walk the included ones in the same order.
    included = [i for i, _ in enumerate(book.chapters)
                if plan.get(i, {}).get("include", True)]

    chapters = []
    for position, index in enumerate(included):
        source = book.chapters[index]
        entry = plan.get(index, {})
        timed = by_chapter.get(position, [])
        cursor = 0
        blocks = []
        for block in source.blocks:
            groups = chunk_paragraphs(
                clean_text(block["text"], drop_citations=drop), MAX_CHUNK_CHARS
            )
            wanted = sum(len(g) for g in groups)
            take = timed[cursor:cursor + wanted]
            cursor += wanted
            # Whether the block carries markup that a sentence boundary could
            # cut through. Where it does, the reader shows the book's own HTML
            # and lights the whole paragraph; where it does not, it can split
            # into sentences and lose nothing. Formatting is never sacrificed —
            # only the precision of the highlight, and only for the minority of
            # paragraphs that are both emphasised and multi-sentence.
            inner = BeautifulSoup(block["html"], "lxml")
            has_inline = bool(inner.find(["em", "i", "strong", "b", "a",
                                          "span", "sup", "sub", "code", "small"]))
            blocks.append({
                "tag": block["tag"],
                "html": block["html"],
                "inline": has_inline,
                "chunks": [
                    {"text": c["text"], "start": c["start"], "end": c["end"],
                     # Present once a narrator that times words has been over
                     # this book. Absent on older ones, which the reader treats
                     # as "highlight the sentence" exactly as it always did.
                     "words": c.get("words") or []}
                    for c in take
                ] if len(take) == wanted else [],
            })
        aligned = cursor == len(timed)
        chapters.append({
            "index": position,
            "title": entry.get("title") or source.title,
            "start": chapter_times.get(position, {}).get("start", 0.0),
            "end": chapter_times.get(position, {}).get("end", 0.0),
            "blocks": blocks if aligned else [
                {"tag": b["tag"], "html": b["html"], "chunks": []}
                for b in source.blocks
            ],
            "aligned": aligned,
        })

    return {"chapters": chapters}


@app.get("/api/jobs/{job_id}/cover")
def job_cover(job_id: uuid.UUID):
    for path in (DATA_DIR / f"uploads/{job_id}").glob("cover.*"):
        # Pin the type rather than letting the filename choose it: covers older
        # than the whitelist above may still be on disk with any extension.
        media_type = COVER_TYPES.get(path.suffix.lower())
        if media_type is None:
            continue
        return FileResponse(path, media_type=media_type)
    return Response(status_code=204)


@app.post("/api/jobs/{job_id}/start")
def start(
    job_id: uuid.UUID,
    narrator: str = Body(...),
    chapters: list[dict] = Body(...),
    mode: str = Body("full"),
    seed: int = Body(1234),
    concurrency: int = Body(2),
    voice_ref_path: str | None = Body(None),
    drop_citations: bool = Body(False),
):
    """Queue a reviewed draft — or a sample of it — for the worker."""
    if mode not in ("full", "sample"):
        raise HTTPException(400, "mode must be 'full' or 'sample'")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise HTTPException(400, f"concurrency must be 1–{MAX_CONCURRENCY}")
    if not any(c.get("include") for c in chapters):
        raise HTTPException(400, "select at least one chapter")

    with pool.connection() as conn:
        draft = conn.execute(
            "SELECT status, epub_filename, epub_path, title, author FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        if draft is None:
            raise HTTPException(404, "job not found")
        if draft["status"] != "draft":
            raise HTTPException(409, f"job is already {draft['status']}")

        if voice_ref_path is not None:
            ref, params = _own_upload(job_id, voice_ref_path), {}
            narrator = "custom"
        else:
            preset = resolve(narrator)
            if preset is None:
                raise HTTPException(400, f"unknown narrator {narrator!r}")
            ref, params = preset["ref"], preset["params"]

        plan = json.dumps(chapters)
        included = sum(1 for c in chapters if c.get("include"))

        if mode == "sample":
            # Samples run as their own row so the draft stays reviewable and
            # the library isn't cluttered with half-books. Only the newest
            # sample per draft is kept — auditioning voices would otherwise
            # leave megabytes of invisible audio behind.
            _drop_samples(conn, job_id)
            sample_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO jobs (id, parent_id, status, mode, epub_filename, epub_path,
                                  title, author, narrator, tts_params, seed,
                                  voice_ref_path, chapters, chapter_count, drop_citations)
                VALUES (%s, %s, 'queued', 'sample', %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                """,
                (sample_id, job_id, draft["epub_filename"], draft["epub_path"],
                 draft["title"], draft["author"], narrator, json.dumps(params), seed,
                 ref, plan, drop_citations),
            )
            return _fetch(conn, sample_id)

        # Converting for real — the auditions have served their purpose.
        _drop_samples(conn, job_id)

        conn.execute(
            """
            UPDATE jobs
            SET status = 'queued', narrator = %s, tts_params = %s, seed = %s,
                voice_ref_path = %s, chapters = %s, chapter_count = %s,
                concurrency = %s, drop_citations = %s, cancel_requested = false,
                error = NULL, updated_at = now()
            WHERE id = %s
            """,
            (narrator, json.dumps(params), seed, ref, plan, included, concurrency,
             drop_citations, job_id),
        )
        return _fetch(conn, job_id)


def _narrator_online(conn) -> bool:
    """Whether any narrator has reported in recently enough to be listening."""
    row = conn.execute(
        "SELECT now() - last_seen < %s * interval '1 second' AS online"
        " FROM workers ORDER BY last_seen DESC LIMIT 1",
        (WORKER_STALE_SECONDS,),
    ).fetchone()
    return bool(row and row["online"])


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: uuid.UUID):
    """Stop a job. Chapters already narrated stay cached, so it can resume."""
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] == "queued":
            # Never claimed, so nothing is running to interrupt.
            conn.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (job_id,))
        elif row["status"] in RUNNING and not _narrator_online(conn):
            # Marked as running, but no narrator has checked in — so there is
            # nobody to notice the flag, and setting it would leave the book
            # saying "Stopping…" for as long as anyone cared to wait. This is
            # the state a narrator leaves behind when it loses the database
            # mid-book: the job it was working is still marked running, and the
            # failure it would have recorded needed the connection that died.
            # Cancel it outright; the audio already narrated stays cached, so
            # Resume picks it up exactly as it would have.
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', cancel_requested = false,"
                " updated_at = now() WHERE id = %s",
                (job_id,),
            )
        elif row["status"] in RUNNING:
            # The worker checks this between chunks and stops within seconds.
            conn.execute(
                "UPDATE jobs SET cancel_requested = true, updated_at = now() WHERE id = %s",
                (job_id,),
            )
        else:
            raise HTTPException(409, f"job is {row['status']}, not running")
        return _fetch(conn, job_id)


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: uuid.UUID):
    """Re-queue a cancelled or failed job; cached chapters are skipped."""
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] not in ("cancelled", "failed"):
            raise HTTPException(409, f"job is {row['status']}")
        conn.execute(
            "UPDATE jobs SET status = 'queued', cancel_requested = false, error = NULL,"
            " updated_at = now() WHERE id = %s",
            (job_id,),
        )
        return _fetch(conn, job_id)


@app.post("/api/jobs/{job_id}/voice", status_code=201)
def upload_voice(job_id: uuid.UUID, voice: UploadFile = File(...)):
    """Attach a custom reference clip to a draft; returns its stored path."""
    ext = Path(voice.filename or "clip.wav").suffix or ".wav"
    rel = f"uploads/{job_id}/voice{ext}"
    _save_upload(voice, DATA_DIR / rel)
    return {"voice_ref_path": rel}


@app.post("/api/jobs/{job_id}/reassemble")
def reassemble(job_id: uuid.UUID, chapters: list[dict] | None = Body(None)):
    """Rebuild the M4B from cached chapter audio — no re-synthesis.

    Use after editing chapter titles or when assembly itself failed.

    Whether the audio is still cached is the worker's business now: it holds
    the recordings on its own disk, reuses what is there and re-narrates
    anything missing. This endpoint therefore queues the job rather than
    refusing it — a check here could only guess, and guessing wrong would
    either block a rebuild that would have worked or promise one that cannot.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] in ("parsing", "synthesizing", "assembling"):
            raise HTTPException(409, "job is currently running")
        if chapters is not None:
            conn.execute("UPDATE jobs SET chapters = %s WHERE id = %s",
                         (json.dumps(chapters), job_id))
        conn.execute(
            "UPDATE jobs SET status = 'queued', error = NULL, progress = 0, updated_at = now()"
            " WHERE id = %s",
            (job_id,),
        )
        return _fetch(conn, job_id)


def _dir_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())


def _disk_breakdown(job_id) -> dict[str, int]:
    """What this server stores for a job: the audiobook and the source EPUB.

    The scratch recordings are no longer counted because they are no longer
    here — the worker keeps them on its own disk. Reporting a cache size of
    zero would be a lie of a different kind, so the field is simply gone, and
    with it the "Free space" button that used to delete files this server can
    no longer reach.
    """
    output = _dir_bytes(DATA_DIR / "output" / str(job_id))
    upload = _dir_bytes(DATA_DIR / "uploads" / str(job_id))
    return {
        "output_bytes": output,
        "disk_bytes": output + upload,
    }


def _disk_bytes(job_id) -> int:
    return _disk_breakdown(job_id)["disk_bytes"]


@app.get("/api/jobs")
def list_jobs():
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs WHERE mode = 'full'"
            " ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    for row in rows:
        row.update(_disk_breakdown(row["id"]))
    return rows


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: uuid.UUID):
    """Remove a book from Vocalis: the row, the upload, cached audio, and M4B.

    Download the M4B first — this does not put anything in the trash.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] in RUNNING:
            raise HTTPException(409, "this book is still being narrated")
        _drop_samples(conn, job_id)
        conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))

    for directory in _job_dirs(job_id):
        shutil.rmtree(directory, ignore_errors=True)
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: uuid.UUID):
    with pool.connection() as conn:
        return _fetch(conn, job_id)


# ------------------------------------------------------------------- worker


def _worker_db_url() -> str:
    """The worker's DB URL with the password deliberately left out.

    The API reaches Postgres at db:5432 inside the Compose network; the worker
    runs outside it, so it needs the published host:port instead.

    The password is *not* included. This endpoint has no authentication, so
    anything it returns is readable by anyone who can reach the port — baking
    the live database password into the download made it a credential
    disclosure rather than a convenience. `install.sh` prompts for it and puts
    it in the service's PGPASSWORD, which libpq reads on its own, so the secret
    is typed on the machine that needs it and never crosses the network.
    """
    url = os.environ["DATABASE_URL"]
    head, _, tail = url.partition("@")
    scheme, _, userinfo = head.partition("://")
    user = userinfo.partition(":")[0]
    _, _, dbname = tail.partition("/")
    return f"{scheme}://{user}@{WORKER_DB_HOSTPORT}/{dbname}"


def _public_api_url(request: Request) -> str:
    """The address the worker machine should call this API on.

    Taken from the Host header of the request that asked, because that is the
    address a browser on the network just used successfully — correct by
    construction, including the port.

    Guessing it from configuration was wrong in exactly the way that is hardest
    to notice: a stack published on 8092 while the container still believed the
    default 8091 handed out a bundle pointing at a port belonging to some other
    service, and the worker installed cleanly and then could not fetch a book.
    """
    override = os.environ.get("PUBLIC_API_URL")
    if override:
        return override.rstrip("/")
    host = request.headers.get("host")
    if host:
        # X-Forwarded-Proto, not the scheme of this request. Behind a reverse
        # proxy the request reaches this container over plain HTTP however the
        # browser arrived, so the scheme here reads "http" on a site served
        # over TLS — and the narrator was then told to call an address that
        # redirects. Small requests survive a redirect; a POST does not. The
        # server answers 301 and closes while the narrator is still sending,
        # and a finished audiobook dies as "Broken pipe" after hours of work.
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        # A proxy chain can send a list; the first entry is the client's.
        scheme = scheme.split(",")[0].strip() or request.url.scheme
        return f"{scheme}://{host}"
    # No Host header at all (a bare HTTP/1.0 client); fall back to the address
    # the worker is already told to reach Postgres on.
    return f"http://{WORKER_DB_HOSTPORT.split(':')[0]}:{WEB_PORT}"


@app.get("/api/worker")
def worker_status(request: Request):
    """What the setup page shows: is a narrator connected, and on what hardware."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT hostname, device, device_name, free_gpu_gb, max_concurrency,
                   version, COALESCE(revision, 1) AS revision, last_seen,
                   now() - last_seen < %s * interval '1 second' AS online
            FROM workers ORDER BY last_seen DESC LIMIT 1
            """,
            (WORKER_STALE_SECONDS,),
        ).fetchone()
    return {
        "worker": row,
        # What this server needs to offer everything it knows how to. A narrator
        # below it still narrates; it just quietly produces books missing
        # whatever it was never taught to record.
        "required_revision": REQUIRED_WORKER_REVISION,
        # Built here so the page never has to assemble an address or a key.
        # Quoted: zsh is the default shell on macOS and treats '?' as a glob,
        # so an unquoted URL with a query string fails with "no matches found"
        # before curl is ever reached.
        "install_command": (
            f'curl -fsSL "{_public_api_url(request)}'
            f'/api/worker/install?key={worker_token()}" | sh'
        ),
    }


@app.get("/api/worker/install")
def worker_install_script(request: Request):
    """A shell script that downloads the bundle and installs it.

    Exists because the download-and-double-click route cannot be made to work:
    anything a browser saves is tagged com.apple.quarantine, and macOS refuses
    to open an unsigned script from Finder with a dialog whose primary button
    is "Move to Trash". Files fetched by curl carry no such tag.

    It also removes two smaller traps — instructions that assume which
    directory you are in, and browsers that silently unzip the download so the
    unzip step fails on a file that is no longer there.

    The server address is written in from the request that fetched this, so the
    script cannot disagree with the page it was copied from.
    """
    api = _public_api_url(request)
    key = worker_token()
    script = f"""#!/bin/sh
# Vocalis narrator installer. Fetches the worker bundle from {api} and runs it.
set -eu

API="{api}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

printf '\\nDownloading the narrator from %s\\n' "$API"
curl -fsSL "$API/api/worker/bundle?key={key}" -o "$TMP/bundle.zip"

# ditto is preferred on macOS for extended attributes, but it does NOT carry a
# zip's executable bit across — verified: ditto yields rw-r--r--, unzip yields
# rwx. Either way the script is invoked through sh below, so the mode cannot
# decide whether the install works.
if command -v unzip >/dev/null 2>&1; then
  mkdir -p "$TMP/unpacked" && unzip -q "$TMP/bundle.zip" -d "$TMP/unpacked"
elif command -v ditto >/dev/null 2>&1; then
  ditto -x -k "$TMP/bundle.zip" "$TMP/unpacked"
else
  printf 'Need unzip or ditto to unpack the bundle.\n' >&2
  exit 1
fi

cd "$TMP/unpacked/vocalis-worker"
chmod +x install.sh 2>/dev/null || true
# Through sh, not ./install.sh: an extractor that dropped the executable bit
# would otherwise stop the install with "permission denied".
exec sh install.sh
"""
    return Response(script, media_type="text/x-shellscript")


@app.get("/api/worker/bundle")
def worker_bundle(request: Request):
    """Zip of the worker source plus a ready-to-run .env, built per download.

    The .env carries this server's DB address and data dir so `install.sh` needs
    no manual configuration — the machine with the GPU just unpacks and runs it.
    """
    if not BUNDLE_ROOT.is_dir():
        raise HTTPException(500, "worker bundle source is missing from the image")

    env = (
        "# Written by the Vocalis setup page — the address of your server.\n"
        "# The password is left out on purpose: this file travels over the\n"
        "# network, so install.sh asks for it on the machine that needs it.\n"
        f"DATABASE_URL={_worker_db_url()}\n"
        "\n"
        "# Books come down from here and finished audio goes back the same way,\n"
        "# so there is no shared folder to mount or keep in step.\n"
        f"VOCALIS_API_URL={_public_api_url(request)}\n"
        "\n"
        "# The narrator's credential. It runs unattended and cannot log in, so\n"
        "# it presents this instead. Downloading this bundle requires being\n"
        "# logged in, which is what keeps the token from being handed out.\n"
        f"VOCALIS_WORKER_TOKEN={worker_token()}\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(BUNDLE_ROOT.rglob("*")):
            name = path.relative_to(BUNDLE_ROOT)
            parts = set(name.parts)
            if parts & {"__pycache__", ".venv", ".git", "node_modules"}:
                continue
            # Ship source and the installer, not dev artifacts (smoke_test.wav,
            # stray audio, compiled bytecode).
            if path.suffix in {".wav", ".pyc", ".m4b", ".log"}:
                continue
            if not path.is_file():
                continue
            # install.sh reasons about paths relative to itself and expects to
            # sit at the bundle root beside core/, worker/ and .env — hoist it
            # out of worker/ so `./install.sh` just works after unpacking.
            arc = Path("vocalis-worker")
            hoist = {"worker/install.sh": "install.sh",
                     "worker/Install Vocalis Narrator.command":
                         "Install Vocalis Narrator.command"}
            arc = arc / hoist.get(name.as_posix(), name)
            z.write(path, arc)
        z.writestr("vocalis-worker/.env", env)
        for entry in ("install.sh", "Install Vocalis Narrator.command"):
            try:
                # Both must stay executable, or Finder opens the .command in a
                # text editor and the shell refuses install.sh.
                z.getinfo(f"vocalis-worker/{entry}").external_attr = 0o755 << 16
            except KeyError:
                pass
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vocalis-worker.zip"'},
    )


@app.get("/api/jobs/{job_id}/download")
def download(job_id: uuid.UUID):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, mode, output_path, title, epub_filename FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] != "done" or not row["output_path"]:
        raise HTTPException(409, "job is not finished")

    path = DATA_DIR / row["output_path"]
    if not path.is_file():
        raise HTTPException(500, "output file missing from data dir")

    if row["mode"] == "sample":
        return FileResponse(path, media_type="audio/wav")
    name = (row["title"] or Path(row["epub_filename"]).stem) + ".m4b"
    return FileResponse(path, media_type="audio/mp4", filename=name)
