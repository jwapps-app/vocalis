# Vocalis

EPUB → M4B audiobook converter. See [vocalis.md](vocalis.md) for the full spec.

Split deployment: web UI + API + Postgres run in Docker; the TTS worker runs
**natively** on the host so Chatterbox gets the Metal (MPS) backend.
The two halves share a Postgres job queue (`FOR UPDATE SKIP LOCKED`) and a
bind-mounted data directory. Paths in the DB are relative to that directory.

## 1. Containerized half

```sh
cp .env.example .env   # set POSTGRES_PASSWORD and an absolute VOCALIS_DATA_DIR
docker compose up -d --build
```

Web UI: <http://localhost:8091>. Postgres is exposed on `127.0.0.1:5445` for
the native worker.

Nothing else needs copying in. The ten narrator voices are baked into the API
image and seeded into the data directory the first time the stack starts —
file by file, never overwriting, so voices added later with `add_narrator.py`
survive restarts. They were synthesized once with Kokoro-82M (Apache-2.0) and
are shipped rather than generated because regenerating them would mean
installing Kokoro, espeak-ng and a spaCy model for a one-time job.

## 2. Native worker (the machine with the GPU)

The easiest install is from the running web UI: open **Setup**, download the
worker bundle (already pointed at this server via a generated `.env`), unpack
it, and run `./install.sh`. It builds the venv, installs the stack, and
registers the launchd/systemd service. The manual steps below are the same
thing by hand.

The Setup page also shows whether a narrator is **connected** and on what
hardware, read from a `workers` heartbeat row the worker upserts every poll —
including *during* synthesis, via the cancel-check that runs every couple of
seconds, so a worker mid-book doesn't read as offline. `GET /api/worker`
returns it; `GET /api/worker/bundle` streams the zip. A silent CPU fallback
(catastrophically slow) is surfaced here rather than hidden.


```sh
cd worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e ../core
brew install ffmpeg   # if not already present

DATABASE_URL="postgresql://vocalis@127.0.0.1:5445/vocalis" \
PGPASSWORD="<your POSTGRES_PASSWORD>" \
VOCALIS_DATA_DIR="$PWD/../data" \
.venv/bin/python -m vocalis_worker.main
```

First run downloads the Chatterbox weights from Hugging Face.

### Run under launchd

`install.sh` writes the LaunchAgent for you — with the right paths for wherever
you unpacked it, and the database password in `PGPASSWORD` on a file created
`chmod 600`. There is no checked-in plist to edit: a template would either
carry someone else's home directory or invite pasting a password into a file
tracked by git.

Run it as a service rather than from a terminal or an editor's task runner: a
child process inherits its parent's memory accounting, so a worker launched
from another app makes *that* app look like the memory hog when synthesis is
heavy, and quitting the app kills the conversion mid-book. Under launchd the
worker is independent, starts at login, and `KeepAlive` restarts it — combined
with the orphan re-queue below, a crash costs only the chapters in flight.

```sh
launchctl print gui/$(id -u)/com.jwapps.vocalis.worker | head -5   # status
launchctl kickstart -k gui/$(id -u)/com.jwapps.vocalis.worker      # restart after a code change
launchctl bootout gui/$(id -u)/com.jwapps.vocalis.worker           # stop and unload
```

Logs: `~/Library/Logs/vocalis-worker.log`.

## 3. Benchmark before a full book

```sh
cd worker
.venv/bin/python benchmark.py path/to/book.epub --chunks 10
```

Synthesizes (part of) a median-length chapter and projects total conversion
time against the 24h target. The worker also updates this estimate live in
`estimated_total_seconds` after each chapter, surfaced as an ETA in the UI.

## Narrators

The web UI's narrator dropdown is served by `GET /api/narrators`:

- **Kokoro-derived presets** — 10 voices (Michael, Puck, Adam, Onyx, George,
  Lewis, Heart, Bella, Sarah, Emma) whose reference clips were synthesized
  once with the Apache-licensed Kokoro-82M model, then cloned by Chatterbox
  at conversion time. Kokoro itself is not needed at runtime; clips live in
  `data/narrators/`. Kokoro has ~28 English voices total if you ever want to
  add more. (Chatterbox's stock "Nova" voice was removed from the menu; a
  manifest entry with `"ref": null` brings it back.)
- **Previews** — the ▶ button in the UI plays a Chatterbox-rendered sample
  (`data/narrators/previews/<id>.wav`, served by
  `GET /api/narrators/{id}/preview`). After adding narrators, render missing
  previews with:

  ```sh
  cd worker
  VOCALIS_DATA_DIR=$PWD/../data .venv/bin/python make_previews.py
  ```
- **Custom clip** — choosing "Custom voice clip…" uploads a 5–30s reference
  sample for zero-shot cloning. Only use voices you have rights to.
- **Add your own presets** — register a reference clip as a reusable narrator:

  ```sh
  cd worker
  .venv/bin/python add_narrator.py my-voice "My Voice" ~/clip.m4a --start 10 --duration 20
  ```

  This writes a normalized WAV plus `manifest.json` under `data/narrators/`;
  the dropdown picks it up immediately.

## Converting a book

Uploading no longer starts synthesis. The flow is **analyze → review → convert**:

1. **Analyze** (`POST /api/jobs/analyze`) parses the EPUB in the API container
   and creates a `draft` job with a chapter plan. The worker ignores drafts.
2. **Review** in the UI. Each chapter shows where its title came from, and
   anything not taken from the book's table of contents is flagged — `from
   heading`, `guessed`, or `unnamed` — so a bad name can be corrected before
   hours of narration. Titles are editable inline. Front matter (contents,
   copyright, index, …) is unticked automatically and skipped.

   A **▸ toggle on each row** reveals that section's opening words, because a
   title routinely fails to say what a section actually is — one book's first
   section is titled with the book's own name and turns out to be the table of
   contents, which the front-matter heuristic does not catch. The text is
   cleaned exactly as the narrator will clean it (and honours the citation
   setting), so it shows what will be read aloud rather than what is in the
   file. `GET /api/jobs/{id}/excerpts` returns all sections in one response, so
   expanding a row costs nothing after the first fetch.
3. **Sample** renders roughly a minute of the first selected chapter in the
   chosen voice, so you can audition a narrator on the actual book. Samples
   are separate job rows (`mode='sample'`) linked to the draft by `parent_id`
   and stay out of the library list. Only the newest sample per draft is
   kept, and all of them are removed when the draft is converted or deleted —
   otherwise auditioning voices would silently accumulate audio on disk.
4. **Convert** (`POST /api/jobs/{id}/start`) queues the reviewed plan.

### Text handling for the voice

**Not every period ends a sentence.** Splitting on all of them cut `Dr. Stuart
Scott` into `Dr.` + `Stuart Scott…`, and a chunk ending on a bare `Dr.` is a
fragment no training sentence ends with — the model fills the gap by inventing
a word, which is how "Dr. Stuart Scott" was narrated as "Dr. carry Stuart
Scott". `J. I. Packer` and `Rom. 12:4` broke the same way. `split_sentences()`
suppresses a break after a known abbreviation, a single-letter initial, a bare
list marker (`1.`, `(3).`), or before a lower-case continuation. On one book
that removed 68 false breaks and cut tiny fragments from 125 to 92.

Changing how text is chunked **invalidates cached audio**: a segment file holds
a particular span of chunks, so after a re-chunk the file named segment 3 no
longer covers the words segment 3 now needs, and reusing it duplicates or drops
a sentence. `_drop_restaled_audio()` fingerprints each chapter's chunk texts and
discards that chapter's segments when the fingerprint changes. Books cached
before fingerprints existed are kept only if complete — a partly-rendered
chapter would splice old segments onto new ones cut at different boundaries.

Chatterbox is autoregressive: on input unlike its training data it hallucinates
plausible-sounding audio rather than failing. `core/vocalis_core/text_clean.py`
rewrites the two constructs that trigger this — spaced/repeated periods
(`. . .`, `...`) collapse to a single `…`, and semicolons soften to commas —
always, since they are pure improvements. Abbreviations (`U.S.`, `Ph.D.`) are
left intact.

**Skip inline references** is an opt-in checkbox on the Review screen
(`drop_citations` on the job). It removes bare in-text citations — scripture
refs (`1 Cor. 12:4–6`), cross-references (`see p. 42`), bare years — which are
both disruptive to hear and exactly the numeric/colon input the model garbles.
Prose in parentheses is always kept; the guard drops a parenthetical only when
it is nothing *but* a citation. On one issue that removed 240 of 250
parentheticals while keeping all ten real asides. Off by default because a
study Bible or commentary wants its references read; a devotional does not.

The checkbox is backed by a preview, so the choice is made from the book rather
than from a guess. `GET /api/jobs/{id}/citations` returns per-chapter counts
plus examples in context, and the review screen shows *"240 references would be
skipped"* with a viewer to page through them one at a time, the reference
highlighted in place. Counts are held per chapter and totalled against the
current tick-boxes, so deselecting a chapter updates the figure immediately.

`find_citations()` and `_strip_citations()` share one predicate, `_is_citation`.
That is the point of the refactor rather than incidental tidiness: a preview
computed from a second, near-identical rule could disagree with what actually
happens, which would be worse than showing nothing. The invariant is checked
directly — on the July issue the preview reports 240 and stripping removes 240.

### Speed: parallel chapters

Chatterbox decodes one audio token at a time (~19 steps/sec on an M4 Pro), so
a single stream leaves most of the GPU idle waiting on sequential
dependencies. The review screen offers **Chapters at once** (1–4); the worker
renders that many chapters in parallel processes, each holding its own copy of
the model.

**Memory.** PyTorch's MPS caching allocator defaults to a high watermark of
1.7× the device's recommended working set — 30 GB on a 24 GB Mac — and
Chatterbox takes it. Measured: a *single* process reserved **25.0 GB** from the
Metal driver while holding only 3.0 GB of live tensors. Unified memory means
those reservations are system RAM, so running several at once exhausted the
machine and macOS began force-quitting applications.

`pool.py` therefore pins `PYTORCH_MPS_HIGH/LOW_WATERMARK_RATIO` before torch is
imported in each pool process. Capping at 4.5 GB dropped the driver reservation
to **3.7 GB** with per-chunk time unchanged (17.2s vs 17.5s over 8 chunks) —
the allocator was hoarding, not using.

A single fixed cap is wrong in both directions, though: 4.5 GB was enough for
the benchmark but a real chapter raised `MPS backend out of memory` mid-book.
So the budget is derived — `(RAM − 8 GB reserve) / concurrency − 2.5 GB` of
process overhead — and `safe_concurrency()` walks the requested value down
until each process still gets `MIN_BUDGET_GB` (8 GB).

**Capacity is measured, not assumed.** Free GPU is whatever the rest of the
desktop leaves behind — it was observed moving between 1.5 GB and 11.8 GB on
the same machine within one afternoon — so a hardcoded figure is wrong in both
directions: it starves a busy machine and wastes an idle one. Before each job
the worker runs a short probe (`identity.refresh()`): on CUDA it reads
`torch.cuda.mem_get_info()`; on MPS, which exposes no such query, it allocates
0.5 GB blocks under the renderers' own ceiling until Metal refuses, then frees
them. That takes under a second and yields real headroom. `safe_concurrency()`
divides it by the measured per-process peak, holding back
`GPU_SAFETY_MARGIN_GB` so the desktop can grow mid-book.

The difference is not academic: with a browser and editor open this Mac
measured ~6 GB free and ran one chapter at a time; with them closed it measured
16.0 GB and ran two. The Setup page shows the number and says so, because
quitting a browser before a long book is a real and otherwise invisible lever.
Per job rather than at startup, since a reading taken at login says nothing
about conditions hours later.

The overhead and minimum are set from real OOM reports, not guesses, and the
first guesses were too low. A process holds ~1.5 GB of non-pool GPU memory
(model weights), and a single chunk of a long chapter reaches ~5.4 GB of live
pool memory — so a process needs ~8 GB to be safe, not the 7 GB first assumed,
and the overhead is 2.5 GB, not 1 GB. The practical consequence: **a 24 GB Mac
runs one chapter at a time** (13.5 GB budget); 32 GB runs two, 48 GB three.
Two long chapters two-up on a 24 GB Mac genuinely does not fit, however
appealing the parallelism — the earlier "two" was optimism the hardware
refused. Chunk size is also capped at 300 characters so no single chunk spikes
toward the ceiling. A chapter that still OOMs is retried once after
`empty_cache()` before the job is failed.

The output is unaffected: every chunk re-seeds with the job's fixed seed and
the same reference clip, so a chapter renders identically regardless of which
process handles it or in what order. Concurrency is purely a
speed-versus-memory trade, which is why it is exposed rather than hardcoded —
1 or 2 suits a base-model or low-memory Mac.

Processes rather than threads: PyTorch's decode loop holds the GIL often
enough that threads barely overlap, and a model instance is not safe to share.

### Crash recovery

A job interrupted by a crash, reboot, or `kill` still reads as `synthesizing`
even though no worker owns it. On startup the worker re-queues anything left
in a running state and continues it, reusing cached chapters. This assumes a
**single worker** — running more than one would need a lease or heartbeat so
they don't steal each other's jobs.

The worker traps `SIGTERM` and raises `SystemExit` rather than letting Python
stop the interpreter outright, so `ChapterPool.__exit__` runs and terminates
the render processes. Without it they are orphaned: the pool children's
command line is `multiprocessing.spawn`, not `vocalis_worker`, so a
`pkill -f vocalis_worker` misses them and they keep holding the GPU, rendering
chapters whose results nobody is listening for. That happened once — two
orphans competed with a freshly started worker for twenty minutes, duplicating
its work. Stop the worker with `launchctl bootout`, never `pkill`.

### Cancelling and resuming

`POST /api/jobs/{id}/cancel` stops a running job — the worker checks the flag
between chunks, so it takes effect within seconds and terminates chapters in
flight. Finished chapters stay cached, so **Resume**
(`POST /api/jobs/{id}/resume`) picks up where it left off instead of starting
over. Cancelled jobs keep the `cancelled` status and can also just be deleted.

### Cached chapter audio

Chapter WAVs are kept in `data/work/<job-id>/` and reused on any later run of
that job, keyed by the chapter's original index. So:

- A job that fails partway **resumes** instead of re-narrating from scratch.
- `POST /api/jobs/{id}/reassemble` ("Rebuild file" in the UI) rewrites the M4B
  from cached audio in seconds — use it after editing chapter titles or when
  only the ffmpeg step failed.

The cache costs roughly 170 MB per hour of audio, so finished books should be
cleared out once you have filed the M4B away:

- **Edit chapters** reopens a finished book's plan: rename sections, or untick
  one that shouldn't be there (a contents page narrated by mistake), then
  rebuild — `POST /api/jobs/{id}/reassemble` with the revised plan. The
  endpoint has always accepted one; until recently nothing in the UI sent it,
  so the button (then "Rebuild file") silently re-packaged with the old titles.

  Two costs hide behind the same tick-box, so the screen separates them.
  *Removing* a section is a repackage: the audio stays on disk, ffmpeg restamps
  the chapter marks, seconds. *Adding* a section that was skipped the first
  time means recording it — minutes to hours. `GET /api/jobs/{id}/recorded`
  reports which chapter indexes have audio, so unrecorded ones are tagged, a
  warning appears when one is ticked, and the button changes to "Save and
  record the missing sections" rather than starting a long job silently.

  The list shows every section but submits the **whole** plan, unticked rows
  included. `apply_plan` treats a chapter index missing from the plan as
  *included*, so posting only the ticked ones would restore exactly what the
  screen just removed.

- **Delete** (`DELETE /api/jobs/{id}`) removes the library entry, the uploaded
  EPUB, the cached audio, and the M4B. There is no trash — download first.
  Books currently being narrated are refused with a 409.
- **Free space** (`DELETE /api/jobs/{id}/cache`) drops just the cached chapter
  audio and keeps the finished M4B in the library. Offered in the UI once a
  book's cache exceeds 200 MB. Rebuild stops working for that book afterward.

Each finished entry shows its playing time, chapter count, how long it took to
narrate, and the size of the M4B.

**"Converted in" is `work_seconds`, not `finished_at - started_at`.** The worker
resets `started_at` every time it claims a job, so the naive subtraction
describes only the run that happened to finish — the July issue, interrupted six
times, reported "converted in 60s" for what was really hours, because the last
run was just the assembly step. `work_seconds` is added to as chapters land, so
it survives crashes and resumes and a crash costs at most one chapter's worth of
tally. Books converted before the column existed show no figure at all rather
than a recovered guess; their playing time was backfilled from the M4B with
`ffprobe`, which is exact.

## Installing as an app (PWA)

The web UI is a progressive web app: installable on desktop and mobile, with its
own window and icon. `web/public/manifest.webmanifest` declares it,
`web/public/sw.js` is a hand-written service worker (no build plugin, no extra
dependency), and the Setup page carries the install button.

**The caching rules are deliberately narrow**, because the worst thing this app
could do is show stale state — a cached "Chapter 5 of 39" during a job that has
moved on, or a cached "narrator offline", would be worse than no PWA at all:

| Request | Strategy | Why |
| --- | --- | --- |
| `/api/*` | never cached | job progress, heartbeat, downloads and previews must be live |
| navigations | network first | a rebuilt UI lands on the next load, not whenever a cache expires |
| `/assets/*` | cache first | Vite fingerprints them, so a URL's bytes never change |
| icons, manifest | stale-while-revalidate | slightly old is harmless |

Range requests pass straight through, so streaming an M4B or a voice preview is
never served a cached partial response. `sw.js` itself is sent `no-store` — a
stale copy of the file that decides all the other caching would pin an installed
app to an old build permanently. Offline is a *graceful failure*, not a feature:
every useful action needs the API and the narrator, so `offline.html` says so
rather than pretending the app works.

### Mobile needs HTTPS

Browsers only register service workers, and only offer installation, in a secure
context: `https://`, or `localhost` (which they exempt). So:

- **On the machine itself** — `http://localhost:8091` installs today.
- **On a phone or another computer** — reaching Vocalis at
  `http://<lan-ip>:8091` is *not* a secure context, so it will not install and
  the service worker will not register. The page still works as an ordinary web
  page. No application code can change this.

The Setup page detects this case and explains it rather than showing a button
that would never appear. To install on mobile, put the server behind TLS —
Tailscale (`tailscale serve` issues a real cert), a Caddy reverse proxy, or a
Cloudflare Tunnel are the usual options.

## Layout

- `docker-compose.yml`, `db/init.sql` — Compose stack and job-table schema
- `core/` — **shared** EPUB parsing and text preparation. Installed into the
  API image and editable-installed into the worker venv (`uv pip install -e
  ../core`) so both halves agree on chapters, titles, and cover art.
- `api/` — FastAPI: analyze, review, start, status, download
- `worker/` — native pipeline: plan → clean → Chatterbox synth → ffmpeg M4B
- `web/` — React/TS UI (nginx serves it and proxies `/api` to the API)
