"""Native TTS worker: claims queued jobs from Postgres and runs the pipeline.

Runs on the host directly (never in Docker — MPS does not pass through).

Books arrive over HTTP and finished audio is posted back, so this machine
shares no filesystem with the server — only the job queue in Postgres. Chapter
audio is cached under the local scratch directory and reused whenever a job
runs again, so a failed, cancelled, or re-assembled job never re-narrates audio
it already has.
"""

import hashlib
import json
import logging
import signal
import time
import traceback
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from vocalis_core.epub_parse import parse_epub
from vocalis_core.text_clean import clean_text, chunk_text

from . import config
from .assemble import assemble_m4b
from .identity import describe, refresh
from .pool import Cancelled, ChapterPool, cached_seconds
from . import transport

log = logging.getLogger("vocalis.worker")

# Progress bands: parsing 0–5, synthesis 5–95, assembly 95–100.
PARSE_DONE, SYNTH_DONE = 5.0, 95.0

# Chunks of the first selected chapter to narrate for an audition. Six ran
# ~2 minutes of audio; three keeps the wait near a minute, which is enough to
# judge a voice.
SAMPLE_CHUNKS = 3


def connect() -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL, row_factory=dict_row, autocommit=True)


def claim_job(conn: psycopg.Connection):
    return conn.execute(
        """
        UPDATE jobs
        SET status = 'parsing', started_at = now(), updated_at = now(),
            cancel_requested = false,
            -- A resumed job inherits an estimate measured on a different run,
            -- at a different concurrency, over work it no longer has to do.
            -- Better to show nothing until this run measures its own rate.
            estimated_total_seconds = NULL
        WHERE id = (
            SELECT id FROM jobs WHERE status = 'queued'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, epub_path, voice_ref_path, seed, tts_params, mode, chapters,
                  concurrency, drop_citations, work_seconds
        """
    ).fetchone()


def requeue_orphans(conn: psycopg.Connection) -> None:
    """Re-queue jobs left mid-flight by a crash, reboot, or kill.

    Those jobs still say 'synthesizing' but no worker owns them any more, so
    without this they would sit stranded forever. Cached chapters mean the
    retry costs nothing for work already done.

    Only correct because Vocalis runs a single worker — with more than one,
    this would steal a live job from its owner and needs a lease/heartbeat.
    """
    rows = conn.execute(
        """
        UPDATE jobs
        SET status = 'queued', cancel_requested = false, updated_at = now()
        WHERE status IN ('parsing', 'synthesizing', 'assembling')
        RETURNING id
        """
    ).fetchall()
    for row in rows:
        log.info("Re-queued %s — it was marked running with nobody narrating it",
                 row["id"])


def heartbeat(conn: psycopg.Connection) -> None:
    """Record that this worker is alive, and what hardware it is using.

    Runs every poll so the setup page can distinguish a connected narrator from
    a stopped one; the API treats a row older than a couple of poll intervals as
    offline.
    """
    w = describe()
    conn.execute(
        """
        INSERT INTO workers
            (id, hostname, device, device_name, free_gpu_gb, max_concurrency,
             version, revision, last_seen)
        VALUES (%(id)s, %(hostname)s, %(device)s, %(device_name)s,
                %(free_gpu_gb)s, %(max_concurrency)s, %(version)s,
                %(revision)s, now())
        ON CONFLICT (id) DO UPDATE SET
            hostname = EXCLUDED.hostname, device = EXCLUDED.device,
            device_name = EXCLUDED.device_name,
            free_gpu_gb = EXCLUDED.free_gpu_gb,
            max_concurrency = EXCLUDED.max_concurrency,
            version = EXCLUDED.version, revision = EXCLUDED.revision,
            last_seen = now()
        """,
        w,
    )


def update(conn: psycopg.Connection, job_id, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(
        f"UPDATE jobs SET {sets}, updated_at = now() WHERE id = %s",
        (*fields.values(), job_id),
    )


def apply_plan(book, plan):
    """Filter and rename parsed chapters using the reviewed plan.

    Returns (chapters, original_indexes) so cached audio keeps stable
    filenames even when the selection changes between runs.
    """
    if not plan:
        return book.chapters, list(range(len(book.chapters)))

    by_index = {entry["index"]: entry for entry in plan}
    chapters, indexes = [], []
    for i, chapter in enumerate(book.chapters):
        entry = by_index.get(i)
        if entry is not None and not entry.get("include", True):
            continue
        if entry and entry.get("title"):
            chapter.title = entry["title"]
        chapters.append(chapter)
        indexes.append(i)
    return chapters, indexes


def _drop_restaled_audio(work_dir, prefix: str, original: int,
                         chunks: list[str], size: int) -> None:
    """Discard a chapter's cached segments when the text was re-chunked.

    A segment file holds the audio for a particular span of chunks. Change how
    text is split into chunks — a sentence-splitter fix, a different
    MAX_CHUNK_CHARS — and segment 3 now covers different words than the file
    named segment 3 contains. Reusing it silently duplicates or drops a
    sentence, which is worse than the cost of re-narrating the chapter, and
    invisible until someone listens.

    Books cached before this existed carry no signature, so there are three
    checks rather than one:

    * the recorded fingerprint no longer matches the chunk texts;
    * a segment numbered beyond what the current chunking produces — a chapter
      that needed eight segments and now needs seven would otherwise lose its
      tail;
    * an unsigned chapter that is only *partly* rendered. A complete one is
      internally consistent whatever chunking produced it, and is worth
      keeping; a partial one would have its finished segments joined to new
      ones cut at different boundaries, which is the case that actually
      duplicates or drops a sentence.
    """
    signature = hashlib.sha256("\x00".join(chunks).encode()).hexdigest()[:16]
    sig_path = work_dir / f"{prefix}_{original:04d}.chunks"
    expected_parts = -(-len(chunks) // size)  # ceil

    present = set()
    for path in work_dir.glob(f"{prefix}_{original:04d}_*.wav"):
        tail = path.stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            present.add(int(tail))

    if sig_path.is_file():
        stale = sig_path.read_text().strip() != signature
    else:
        # Unsigned: trust it only if it is a complete chapter.
        stale = bool(present) and present != set(range(expected_parts))
    if not stale:
        stale = any(part >= expected_parts for part in present)

    if stale:
        removed = 0
        for path in work_dir.glob(f"{prefix}_{original:04d}_*.wav"):
            path.unlink(missing_ok=True)
            removed += 1
        log.info("Chapter %d was re-chunked; dropped %d stale segment(s)",
                 original, removed)
    sig_path.write_text(signature)


def fetch_voice(job, work_dir: Path) -> Path | None:
    """Download the reference clip this job narrates with, if it has one.

    Two sources, and the job says which: a clip uploaded for this book alone,
    or one of the presets. Cached per job because the same clip is used for
    every chapter and a resumed job should not re-fetch it.
    """
    ref = job.get("voice_ref_path")
    if not ref:
        return None
    dest = work_dir / "voice.wav"
    if dest.is_file():
        return dest
    # Custom clips live under the job; presets are named in the manifest.
    if ref.startswith("uploads/"):
        found = transport.download(f"/api/jobs/{job['id']}/voice", dest)
    else:
        narrator = Path(ref).stem
        found = transport.download(f"/api/narrators/{narrator}/clip", dest)
    if not found:
        raise RuntimeError(f"the server has no reference clip for {ref!r}")
    return dest



def _timings_path(seg_path: Path) -> Path:
    """The sidecar for one segment, named after the audio it describes.

    Beside the file rather than keyed by position in the chapter list, because
    those are different numbering schemes: audio is named by the chapter's
    original index in the book, which never moves, while position shifts the
    moment a chapter is deselected. A sidecar keyed by position would then be
    read for a chapter it was never written for — offsets belonging to a
    different piece of text, and a reader confidently highlighting the wrong
    line. Naming it after the audio makes that impossible to express.
    """
    return seg_path.with_suffix(".timings.json")


def _save_segment_timings(seg_path: Path, duration: float, timings) -> None:
    """Keep a segment's timings beside its audio.

    Cached for the same reason the audio is: a book finished across two runs
    would otherwise end up with timings only for the segments the final run
    happened to render, and there is no way to recompute the rest short of
    narrating them again.
    """
    payload = {"duration": duration,
               "chunks": [{"text": t, "start": s, "end": e} for t, s, e in timings]}
    _timings_path(seg_path).write_text(json.dumps(payload))


def _rewrite_segment_words(seg_path: Path, chunks: list) -> None:
    """Persist newly aligned words back into the segment's sidecar.

    So the alignment is paid for once. Without it a resumed or rebuilt book
    would re-align every segment it had already done.
    """
    path = _timings_path(seg_path)
    try:
        payload = json.loads(path.read_text()) if path.is_file() else {}
        payload["chunks"] = chunks
        path.write_text(json.dumps(payload))
    except (OSError, ValueError) as exc:
        log.warning("Could not save word timings for %s (%s)", path.name, exc)


def _load_segment_timings(seg_path: Path) -> list | None:
    path = _timings_path(seg_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())["chunks"]
    except (OSError, ValueError, KeyError):
        return None


# A shade under the 0.4s pause the synth inserts, so a boundary cannot be
# missed to rounding, and well above anything that occurs inside speech.
_PAUSE_FLOOR_SECONDS = 0.30


def _derive_segment_timings(path: Path, chunks: list[str] | None) -> list | None:
    """Recover a segment's chunk timings from the audio itself.

    For books narrated before timings were recorded. Chunks are joined with a
    pause of *digital* silence — literal zero samples, not a quiet passage — so
    the boundaries are still in the file exactly where they were put, and
    finding them is a scan rather than an estimate. That turns "narrate the
    whole book again" into a pass over audio already on disk: minutes instead
    of a day.

    Returns None unless the pauses divide the segment into exactly as many
    pieces as it had chunks. Any disagreement means this audio did not come
    from this chunking, and a reader that highlights the wrong sentence is
    worse than one that highlights nothing.
    """
    if not chunks or not path.is_file():
        return None
    try:
        import numpy as np
        import soundfile as sf

        audio, rate = sf.read(str(path), dtype="float32")
    except Exception:
        return None
    if getattr(audio, "ndim", 1) > 1:
        audio = audio[:, 0]

    silent = audio == 0.0
    edges = np.diff(silent.astype(np.int8))
    starts = np.flatnonzero(edges == 1) + 1
    ends = np.flatnonzero(edges == -1) + 1
    if silent[0]:
        starts = np.r_[0, starts]
    if silent[-1]:
        ends = np.r_[ends, len(audio)]

    floor = int(_PAUSE_FLOOR_SECONDS * rate)
    spoken: list[tuple[float, float]] = []
    cursor = 0
    for start, end in zip(starts, ends):
        if end - start < floor:
            continue
        if start > cursor:
            spoken.append((cursor / rate, start / rate))
        cursor = end
    if cursor < len(audio):
        spoken.append((cursor / rate, len(audio) / rate))

    if len(spoken) != len(chunks):
        log.warning(
            "%s: %d chunks but %d spoken runs — leaving it untimed",
            path.name, len(chunks), len(spoken),
        )
        return None
    return [
        {"text": text, "start": round(start, 3), "end": round(end, 3)}
        for text, (start, end) in zip(chunks, spoken)
    ]


def _add_words(seg_path: Path, chunks: list) -> bool:
    """Fill in word timings for a segment's chunks, in place.

    Done here rather than during synthesis because everything it needs — the
    audio and the text that made it — is on disk either way. A book narrated
    before words existed is therefore no different from one being made now:
    both are a pass over recordings that already exist, which is why this can
    be backfilled by rebuilding rather than by narrating again.

    Returns whether anything was added, so the caller knows if the sidecar is
    worth rewriting. Chunks that already carry words are left alone.
    """
    import torchaudio
    from .align import align_words

    todo = [c for c in chunks if not c.get("words")]
    if not todo or not seg_path.is_file():
        return False
    try:
        audio, sample_rate = torchaudio.load(str(seg_path))
    except Exception as exc:                        # noqa: BLE001
        log.warning("Could not read %s for word timings (%s)", seg_path.name, exc)
        return False

    added = False
    for chunk in todo:
        clip = audio[:, int(chunk["start"] * sample_rate):
                        int(chunk["end"] * sample_rate)]
        if clip.shape[1] < sample_rate // 20:       # under 50ms, nothing to align
            continue
        words = align_words(clip, sample_rate, chunk["text"])
        if words:
            # Stored relative to the segment, like the chunk itself, so the
            # book-wide offset is applied once in build_timeline.
            for w in words:
                w["start"] = round(chunk["start"] + w["start"], 3)
                w["end"] = round(chunk["start"] + w["end"], 3)
            chunk["words"] = words
            added = True
    return added


def build_timeline(chapters, segments, seg_seconds, seg_timings, work_dir) -> dict:
    """Turn per-segment timings into offsets within the finished audiobook.

    Segments are concatenated in order, so a chunk's place in the book is its
    place in its segment plus everything before it. Chapters get their own
    entries so a player can offer a chapter list — the marks written into the
    M4B are unreadable to a browser's <audio>.
    """
    book_chunks, book_chapters = [], []
    offset = 0.0
    aligned = 0
    for position, units in enumerate(segments):
        chapter_start = offset
        for part, (seg_path, seg_chunks) in enumerate(units):
            key = (position, part)
            # Just rendered, then the sidecar from an earlier run, and only
            # then the audio itself — the last is for books whose segments were
            # narrated before any timings were kept.
            chunks = (
                seg_timings.get(key)
                or _load_segment_timings(seg_path)
                or _derive_segment_timings(seg_path, seg_chunks)
                or []
            )
            if _add_words(seg_path, chunks):
                _rewrite_segment_words(seg_path, chunks)
                aligned += 1
                if aligned % 20 == 0:
                    log.info("Timed the words in %d segments so far", aligned)
            for c in chunks:
                book_chunks.append({
                    "chapter": position,
                    "text": c["text"],
                    "start": round(offset + c["start"], 3),
                    "end": round(offset + c["end"], 3),
                    "words": [
                        {"text": w["text"],
                         "start": round(offset + w["start"], 3),
                         "end": round(offset + w["end"], 3)}
                        for w in c.get("words", ())
                    ],
                })
            offset += seg_seconds.get(key, 0.0)
        book_chapters.append({
            "index": position,
            "title": chapters[position].title,
            "start": round(chapter_start, 3),
            "end": round(offset, 3),
        })
    if aligned:
        log.info("Timed the words in %d segment(s)", aligned)
    return {"chapters": book_chapters, "chunks": book_chunks}


def process_job(conn: psycopg.Connection, job) -> None:
    job_id = job["id"]
    is_sample = job["mode"] == "sample"
    log.info("Processing %s job %s", job["mode"], job_id)

    # Everything below lives on this machine's own disk. The book comes down
    # over HTTP and the finished file goes back the same way, so no directory
    # is shared with the server.
    work_dir = config.WORK_DIR / "work" / str(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)

    epub_path = work_dir / "book.epub"
    if not epub_path.is_file():
        if not transport.download(f"/api/jobs/{job_id}/epub", epub_path):
            raise RuntimeError("the server has no EPUB for this job")

    voice_ref = fetch_voice(job, work_dir)

    book = parse_epub(epub_path)
    if not book.chapters:
        raise RuntimeError("no synthesizable chapters found in EPUB")

    chapters, indexes = apply_plan(book, job.get("chapters"))
    if not chapters:
        raise RuntimeError("the chapter plan excluded every section")
    if is_sample:
        chapters, indexes = chapters[:1], indexes[:1]

    drop_citations = bool(job.get("drop_citations"))
    chapter_chunks = [
        chunk_text(clean_text(c.text, drop_citations=drop_citations), config.MAX_CHUNK_CHARS)
        for c in chapters
    ]
    if is_sample:
        chapter_chunks = [chapter_chunks[0][:SAMPLE_CHUNKS]]

    prefix = "sample" if is_sample else "chapter"
    params = job.get("tts_params") or {}

    cover_path = None
    if book.cover:
        cover_path = work_dir / f"cover{book.cover_ext}"
        cover_path.write_bytes(book.cover)

    # Each chapter renders as one or more fixed-size segments, so a process's
    # lifetime is bounded by chunks rather than by chapters — see
    # pool.SEGMENTS_PER_PROCESS. A book with 40k-character chapters would
    # otherwise hand a single process more work than its GPU graph cache can
    # survive.
    segments: list[list[tuple[Path, list[str] | None]]] = []
    for original, chunks in zip(indexes, chapter_chunks):
        whole = work_dir / f"{prefix}_{original:04d}.wav"
        if cached_seconds(whole) is not None:
            # Rendered before segmenting existed. Keep it: re-narrating a
            # cached chapter to change its filename would be hours wasted.
            segments.append([(whole, None)])
            continue
        size = max(1, config.SEGMENT_CHUNKS)
        _drop_restaled_audio(work_dir, prefix, original, chunks, size)
        segments.append([
            (work_dir / f"{prefix}_{original:04d}_{i // size:03d}.wav", chunks[i:i + size])
            for i in range(0, len(chunks), size)
        ])

    # Reuse anything already rendered; only the rest costs GPU time.
    seg_seconds: dict[tuple[int, int], float] = {}
    # Where each chunk of text lands inside its segment. Cached beside the
    # audio so a resumed job keeps the timings of segments it is not re-doing —
    # otherwise a book finished across two runs would have timings only for
    # whatever the last run happened to render.
    seg_timings: dict[tuple[int, int], list] = {}
    todo = []
    for position, units in enumerate(segments):
        for part, (path, chunks) in enumerate(units):
            cached = cached_seconds(path)
            if cached is not None:
                seg_seconds[(position, part)] = cached
            elif chunks:
                # Every segment but a chapter's last ends with the same pause
                # that separates chunks, so joining them is seamless.
                trailing = part < len(units) - 1
                todo.append((
                    (position, part), chunks, voice_ref, job["seed"], path,
                    params, trailing,
                ))

    def chapter_done(position: int) -> bool:
        return all((position, p) in seg_seconds for p in range(len(segments[position])))

    def chapters_complete() -> int:
        return sum(chapter_done(p) for p in range(len(segments)))

    # Measure GPU headroom now rather than trusting a reading from worker
    # startup: what is free depends on whatever else the desktop is running,
    # and that changes over the hours between login and this book. Samples are
    # short and always single-process, so they skip the probe.
    free_gpu_gb = None
    if not is_sample:
        free_gpu_gb = refresh().get("free_gpu_gb")
    concurrency = 1 if is_sample else max(1, job.get("concurrency") or 1)
    total_chars = sum(sum(len(c) for c in chunks) for chunks in chapter_chunks) or 1
    todo_chars = sum(sum(len(c) for c in t[1]) for t in todo) or 1

    update(
        conn, job_id,
        status="synthesizing", title=book.title, author=book.author,
        chapter_count=len(chapters), chapters_done=chapters_complete(),
        progress=PARSE_DONE
        + (SYNTH_DONE - PARSE_DONE) * (total_chars - todo_chars) / total_chars,
    )
    log.info(
        "%d/%d chapters cached; narrating %d segment(s) with concurrency %d",
        chapters_complete(), len(chapters), len(todo), concurrency,
    )

    # Time already banked by earlier runs of this job. Every run adds to it
    # rather than replacing it, so "converted in" reports the whole effort
    # instead of whatever was left after the last crash.
    work_base = float(job.get("work_seconds") or 0.0)
    run_started = time.monotonic()

    if todo:
        started = run_started
        done_chars = 0

        def cancelled() -> bool:
            # The pool calls this every couple of seconds while rendering, which
            # makes it the one place that reliably runs *during* a long chapter.
            # Heartbeat here too, or a worker mid-book (minutes between job-loop
            # iterations) would read as offline on the setup page.
            heartbeat(conn)
            return bool(
                conn.execute(
                    "SELECT cancel_requested FROM jobs WHERE id = %s", (job_id,)
                ).fetchone()["cancel_requested"]
            )

        def on_done(key: tuple[int, int], duration: float, timings=()) -> None:
            nonlocal done_chars
            position, part = key
            seg_seconds[key] = duration
            # Normalised to the same shape the cached file uses. The pool hands
            # back tuples; a resumed job reads dicts from disk. build_timeline
            # sees both, so they have to agree here rather than there.
            seg_timings[key] = [
                {"text": t, "start": a, "end": b} for t, a, b in timings
            ]
            _save_segment_timings(segments[position][part][0], duration, timings)
            done_chars += sum(len(c) for c in segments[position][part][1] or ())
            # Only announce a chapter once every one of its segments has landed;
            # a half-rendered chapter is not progress the reader can use.
            if chapter_done(position):
                log.info("Finished %s (%d/%d)", chapters[position].title,
                         chapters_complete(), len(chapters))
            elapsed = time.monotonic() - started
            # Project over the work actually left to do, then add what is
            # already on disk — cached audio costs no time.
            update(
                conn, job_id,
                chapters_done=chapters_complete(),
                progress=PARSE_DONE
                + (SYNTH_DONE - PARSE_DONE)
                * (total_chars - todo_chars + done_chars) / total_chars,
                estimated_total_seconds=elapsed / done_chars * todo_chars,
                # Banked as we go: a crash then loses one chapter's worth of
                # tally, not the run's.
                work_seconds=work_base + elapsed,
            )

        with ChapterPool(concurrency, free_gpu_gb) as pool:
            pool.render(todo, on_done, cancelled)

    update(conn, job_id, status="assembling", progress=SYNTH_DONE)
    # ffmpeg concatenates the segments in order, so a chapter's mark spans the
    # sum of its segments — the listener sees chapters, not the split.
    paths = [path for units in segments for path, _ in units]
    chapter_meta = [
        (
            chapters[position].title,
            sum(seg_seconds[(position, part)] for part in range(len(units))),
        )
        for position, units in enumerate(segments)
    ]

    if is_sample:
        out_path = work_dir / "sample.wav"
        out_path.write_bytes(paths[0].read_bytes())
    else:
        out_path = work_dir / "book.m4b"
        assemble_m4b(paths, chapter_meta, book.title, book.author, cover_path,
                     work_dir, out_path)

    # Hand the finished file to the server, which records where it stored it.
    # Uploading before the job is marked done means a failed transfer surfaces
    # as a failed job rather than one that claims to have an audiobook nobody
    # can download.
    log.info("Uploading %s (%.0f MB)", out_path.name, out_path.stat().st_size / 1e6)
    transport.upload(f"/api/jobs/{job_id}/output", out_path)

    update(
        conn, job_id,
        status="done", progress=100.0,
        # output_path is set by the upload endpoint — the server decides where
        # it keeps the file, and only it knows the path was really written.
        #
        # The M4B is a straight concatenation, so the chapter durations sum to
        # its playing time exactly.
        audio_seconds=sum(seg_seconds.values()),
        timings=json.dumps(
            build_timeline(chapters, segments, seg_seconds, seg_timings, work_dir)
        ),
        work_seconds=work_base + (time.monotonic() - run_started),
    )
    conn.execute("UPDATE jobs SET finished_at = now() WHERE id = %s", (job_id,))
    log.info("Job %s done -> %s", job_id, out_path)


def _exit_on_sigterm(signum, frame):
    """Turn SIGTERM into a normal unwind so the pool children go with us.

    Python's default SIGTERM handling stops the interpreter without running
    atexit handlers or context managers, which leaves the spawned render
    processes orphaned — they keep holding the GPU and finish chapters whose
    results nobody is listening for. Raising SystemExit instead runs
    ChapterPool.__exit__ on the way out, terminating them.

    SystemExit is a BaseException, so the `except Exception` around
    process_job deliberately does not catch it. The job stays 'synthesizing'
    and requeue_orphans() picks it up on the next start.
    """
    log.info("Received signal %s, stopping", signum)
    raise SystemExit(0)


def run() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    log.info("Worker ready (api=%s, scratch=%s)", config.API_URL, config.WORK_DIR)
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)

    startup = True
    while True:
        try:
            with connect() as conn:
                if startup:
                    requeue_orphans(conn)
                    startup = False
                heartbeat(conn)
                job = claim_job(conn)
                if job is None:
                    # Nothing to claim — so this narrator is not working on
                    # anything, and with a single narrator that makes every job
                    # still marked running an orphan by definition.
                    #
                    # Tying the rescue to startup alone was not enough. Losing
                    # the database mid-book strands the job that was running:
                    # the handler that would mark it failed needs the very
                    # connection that died, so nothing is recorded, and
                    # claim_job only ever takes a `queued` row. The job could
                    # then be neither narrated nor cancelled — it just said
                    # "Stopping…" until someone restarted the worker. Checking
                    # here catches that within one poll, and covers the idle
                    # case too, where the connection is replaced each pass and
                    # the loss is never even seen as an error.
                    requeue_orphans(conn)
                    time.sleep(config.POLL_INTERVAL_SECONDS)
                    continue
                try:
                    process_job(conn, job)
                except Cancelled:
                    log.info("Job %s cancelled; finished chapters kept", job["id"])
                    update(conn, job["id"], status="cancelled", cancel_requested=False)
                except Exception:
                    log.exception("Job %s failed", job["id"])
                    update(conn, job["id"], status="failed",
                           error=traceback.format_exc()[-4000:])
        except psycopg.OperationalError as exc:
            log.warning("Database unavailable (%s), retrying in 10s", exc)
            time.sleep(10)


if __name__ == "__main__":
    run()
