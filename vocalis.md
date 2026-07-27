# Vocalis — EPUB → Audiobook Converter

Self-hosted service that converts EPUB files into M4B audiobooks using local
neural TTS, targeting the most natural voice quality achievable on Apple Silicon.

## Goal

Upload an EPUB via a web UI, get back a chaptered M4B audiobook. A full-length
novel (~100k words / ~11 hours of audio) must convert in well under 24 hours.

## Architecture

This project has a **split deployment** because Apple's Metal/MPS GPU does NOT
pass through to Docker containers on macOS. Containerized inference would be
CPU-only and too slow for the quality model. Therefore:

- **Web UI + API + job queue → Docker** (standard fleet pattern, Compose stack)
- **TTS worker → runs natively on the host** (not containerized), using the
  Metal backend directly for GPU/Neural Engine acceleration

The two halves communicate over the job queue. Do not attempt to run the TTS
worker inside Docker on macOS — this is a deliberate architectural constraint,
not an oversight.

### Components

1. **Web UI** — upload EPUB, poll job status/progress, download finished M4B.
   React/TypeScript.
2. **API service** — FastAPI. Accepts uploads, enqueues jobs, exposes status
   and download endpoints. Runs in Docker.
3. **Job store** — PostgreSQL table tracking job state
   (`queued → parsing → synthesizing → assembling → done / failed`), progress
   percentage, and chapter counts. Runs in Docker.
4. **TTS worker** — native Python process on the host, managed by `launchd`.
   Polls the queue, does the heavy synthesis, writes output back.

## Pipeline

1. **Parse EPUB** — extract chapters and text (`ebooklib` + `BeautifulSoup`).
2. **Clean text** — strip footnotes, page numbers, artifacts; normalize
   whitespace; split into synthesizable chunks.
3. **TTS synthesis** — Chatterbox (Resemble AI, MIT license) via Metal backend.
   Synthesize per chunk/chapter.
4. **Assembly** — `ffmpeg` concatenates audio and writes an M4B with chapter
   markers and metadata (title, author, cover from the EPUB).

## TTS model

- **Chatterbox** (Resemble AI) — MIT licensed, ~0.5B params, strong naturalness,
  supports zero-shot voice cloning from a short reference clip.
- Each job takes an optional **reference voice clip** (5–30s, clean single
  speaker). Default to a built-in/preset voice when none is supplied.
- For long-book consistency: pin the same reference clip AND a fixed random seed
  across every chunk of a given book to minimize tone drift between chapters.

### Voice cloning notes

- Cloning is zero-shot (inference-time embedding), not training. A clean 10–30s
  sample is enough.
- **Only clone voices you have rights to**: the user's own voice, preset voices,
  or voices with explicit consent. Do not build workflows that default to
  cloning third-party narrators from commercial audiobooks.

## Performance

- Chatterbox is too heavy for real-time on CPU; native Metal execution is what
  keeps a novel under the 24h target.
- **Before full-book runs, benchmark one representative chapter** and project
  linearly to estimate total time. Surface this estimate in the job status.

## Conventions

- Repo under the `jwapps-app` GitHub org.
- **No AI attribution** anywhere in committed content or commit messages.
- No push notifications — job completion is visible in the web UI (decided
  2026-07-22; the shared `push-relay` container is not used here).
- Standard stack: FastAPI / Python, React / TypeScript, PostgreSQL, Docker
  Compose for the containerized half.

## Deliverables to build

- [x] `docker-compose.yml` for web UI + API + Postgres
- [x] FastAPI service: upload, enqueue, status, download endpoints
- [x] Postgres schema / migration for the job table (`db/init.sql`)
- [x] EPUB parsing + text-cleaning module (`worker/vocalis_worker/`)
- [x] Native TTS worker skeleton (queue poll → Chatterbox synth → ffmpeg M4B)
- [x] `launchd` plist for the native worker (`worker/launchd/`)
- [x] Chapter-benchmark script (`worker/benchmark.py`)
- [x] Web frontend: upload form, progress view, download link (`web/`)
