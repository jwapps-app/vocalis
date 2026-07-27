import io
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse

from vocalis_core.epub_parse import parse_epub
from vocalis_core.text_clean import clean_text, find_citations

from .db import pool
from .narrators import list_narrators, resolve

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

app = FastAPI(title="Vocalis API")

JOB_COLUMNS = """
    id, status, epub_filename, title, author, seed, narrator, mode, chapters,
    concurrency, cancel_requested,
    chapter_count, chapters_done, progress, estimated_total_seconds,
    work_seconds, audio_seconds,
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

# A worker whose heartbeat is older than this reads as offline. Comfortably
# above the worker's poll interval so a busy narrator is never called dead.
WORKER_STALE_SECONDS = 30


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
    """
    work_dir = DATA_DIR / "work" / str(job_id)
    if not work_dir.is_dir():
        return {"indexes": []}
    found = set()
    for path in work_dir.glob("chapter_*.wav"):
        # chapter_0004.wav (whole) and chapter_0004_002.wav (segment) both carry
        # the original chapter index in the first field.
        digits = path.stem.split("_")[1] if "_" in path.stem else ""
        if digits.isdigit():
            found.add(int(digits))
    return {"indexes": sorted(found)}


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
    """
    work_dir = DATA_DIR / "work" / str(job_id)
    if not any(work_dir.glob("chapter_*.wav")):
        raise HTTPException(409, "no cached chapter audio for this job")

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
    """Split a job's footprint into the audiobook you keep and the cache you
    can reclaim. Shown separately because they mean different things: the M4B
    in `output/` is the deliverable, the chapter WAVs in `work/` are scratch
    that 'Free space' removes.
    """
    output = _dir_bytes(DATA_DIR / "output" / str(job_id))
    cache = _dir_bytes(DATA_DIR / "work" / str(job_id))
    upload = _dir_bytes(DATA_DIR / "uploads" / str(job_id))
    return {
        "output_bytes": output,
        "cache_bytes": cache,
        "disk_bytes": output + cache + upload,
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


@app.delete("/api/jobs/{job_id}/cache", status_code=200)
def clear_cache(job_id: uuid.UUID):
    """Drop cached chapter audio but keep the finished M4B and the library entry.

    Reclaims most of the disk; the trade-off is that "Rebuild file" will no
    longer work for this book without a full re-narration.
    """
    with pool.connection() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] in RUNNING:
            raise HTTPException(409, "this book is still being narrated")

    shutil.rmtree(DATA_DIR / "work" / str(job_id), ignore_errors=True)
    return {"disk_bytes": _disk_bytes(job_id)}


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


@app.get("/api/worker")
def worker_status():
    """What the setup page shows: is a narrator connected, and on what hardware."""
    with pool.connection() as conn:
        row = conn.execute(
            """
            SELECT hostname, device, device_name, free_gpu_gb, max_concurrency,
                   version, last_seen,
                   now() - last_seen < %s * interval '1 second' AS online
            FROM workers ORDER BY last_seen DESC LIMIT 1
            """,
            (WORKER_STALE_SECONDS,),
        ).fetchone()
    return {"worker": row}


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
        "# Where the worker keeps voice models and cached audio.\n"
        "VOCALIS_DATA_DIR=$HOME/vocalis-data\n"
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
            arc = arc / ("install.sh" if name.as_posix() == "worker/install.sh" else name)
            z.write(path, arc)
        z.writestr("vocalis-worker/.env", env)
        info = z.getinfo("vocalis-worker/install.sh")
        info.external_attr = 0o755 << 16  # keep the installer executable
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
