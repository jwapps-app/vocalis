# Vocalis

Turn an EPUB into a chaptered M4B audiobook, narrated on your own machine.
Nothing is sent to a cloud service.

**In practice this needs a Mac.** The narration runs on Apple Silicon's GPU
through Metal, which is what makes a full-length book take hours rather than
days. The server half runs anywhere Docker does — a NAS is a good home for it —
but the narrator wants a Mac.

There is a CUDA path in the worker and a systemd unit in the installer, and the
memory budgeting reads real figures from `torch.cuda.mem_get_info()`. It is
**untested** — nobody has run it end to end — so treat Linux and NVIDIA as a
starting point rather than a supported configuration. Reports welcome. CPU-only
works and is far too slow to finish a book.

Split deployment, because Metal does not pass through to Docker on macOS:

- **Server** — web UI, API and Postgres, in Docker.
- **Narrator** — a native process on the Mac, installed with one command.

They talk over HTTP and a Postgres job queue. The narrator has no access to the
server's filesystem: it fetches the book, keeps its own scratch on local disk,
and posts the finished audiobook back. So the two halves can be different
machines with nothing shared between them.

## 1. The server

```sh
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up -d
```

That pulls prebuilt images — this file plus `.env` is the whole install, so it
can be pasted straight into Portainer. To build from this checkout instead:

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Open <http://localhost:8091>. **The first page asks you to choose a username
and password**; Vocalis is locked to that single account from then on. Set it
before putting the server anywhere other people can reach.

Upgrading an instance that predates usernames? It keeps working, signs in on
the password alone, and asks you to pick a username once — your password is
unchanged. No default name is assigned, because `admin` on every Vocalis that
ever upgraded would be half the credential given away.

If the narrator will run on a *different* machine from the server, also set
`DB_BIND` and `WORKER_DB_HOSTPORT` — see `.env.example`. Otherwise Postgres
stays on loopback and is not on the network at all.

Nothing else needs copying in. The ten narrator voices are baked into the API
image and seeded into the data directory the first time the stack starts —
file by file, never overwriting, so voices added later with `add_narrator.py`
survive restarts. They were synthesized once with Kokoro-82M (Apache-2.0) and
are shipped rather than generated because regenerating them would mean
installing Kokoro, espeak-ng and a spaCy model for a one-time job.

## 2. The narrator (the Mac)

Open **Setup** in the web UI and run the command it shows you. It is one line,
already carrying your server's address and an enrolment key:

```sh
curl -fsSL "http://<your-server>:8091/api/worker/install?key=..." | sh
```

The only question it asks is the database password — `POSTGRES_PASSWORD` from
the server's `.env`. It checks that before downloading PyTorch, so a wrong
paste costs a second rather than several minutes.

Everything else it works out. It finds a Python version PyTorch actually
supports (the system `python3` is routinely too new — 3.13 on a current Mac,
where the install dies partway through with a compiler error and no mention of
versions), fetching one only if the machine has none. It installs `ffmpeg` if
Homebrew is present. It installs itself to
`~/Library/Application Support/Vocalis/narrator` and registers a launch agent,
so the narrator starts with the machine.

It needs no access to the server's files. Books come down over HTTP and the
finished audiobook goes back the same way.

**Keep it up to date.** The narrator is installed separately and does not
update itself, so a newer server can be running against an older narrator. It
reports what it supports, and the library says so plainly when it is behind —
re-run the same install command to bring it forward. It keeps the existing
virtualenv, so this does not re-download PyTorch.

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
VOCALIS_API_URL="http://127.0.0.1:8091" \
VOCALIS_WORKER_TOKEN="<from the Setup page's install command>" \
.venv/bin/python -m vocalis_worker.main
```

Scratch space goes to `~/Library/Application Support/Vocalis` unless
`VOCALIS_WORK_DIR` says otherwise. It is purely local — nothing there is shared
with the server.

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

## Security, honestly

Vocalis is built for a home network. Know what it does and does not do before
putting it anywhere else.

- **One account.** A username and password, chosen on first load and stored as
  a bcrypt hash. There are no roles and no second user. Everything except the
  login and the narrator's own enrolment is closed by default.
- **Sign-in attempts are rate limited.** Five free tries, then a doubling pause
  up to thirty seconds, forgotten after fifteen minutes of quiet. The count is
  kept for the instance rather than per client address, because behind a
  reverse proxy the address is a header the caller controls — blocking on it
  would look stricter and stop nothing. The trade is that someone hammering
  the login can make you wait up to thirty seconds; it is a pause, not a
  lockout.
- **A wrong username and a wrong password are indistinguishable** — same
  message, and the password is hashed either way so the answer takes the same
  time. Measured at 199 ms against 192 ms.
- **The session is a cookie** (`HttpOnly`, `SameSite=Lax`), not a bearer token,
  because `<audio>`, `<img>` and download links cannot send an `Authorization`
  header and the player and reader depend on them.
- **No TLS of its own.** It serves plain HTTP. On a LAN that means the password
  crosses the network in the clear, and browsers will not install the PWA or
  register a service worker outside a secure context. Put it behind something
  that terminates TLS — Tailscale, Caddy, a Cloudflare Tunnel — before exposing
  it beyond your own network. The Setup page explains this where it bites.
- **The database port** is on loopback unless you set `DB_BIND`. Once you do,
  `POSTGRES_PASSWORD` is the only thing protecting it, so generate a real one.
- **The enrolment key** in the install command is the worker token. Anyone who
  has it can register a narrator and pull books; treat the command as a secret
  and do not paste it into an issue.
- **EPUB markup is sanitized** before the reader renders it — tags reduced to a
  known-safe set, every attribute but `href`/`title` dropped, and non-http(s)
  URLs stripped — because an EPUB is an untrusted document that arrives as HTML.

Found something? Open an issue, or email the address on the commit history if
it is sensitive.

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
directly: on the book above the preview reports 240 and stripping removes 240.

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

The difference is not academic: with a browser and editor open, one 24 GB Mac
measured ~6 GB free and ran a chapter at a time; with them closed it measured
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
even though no worker owns it. The worker re-queues anything left in a running
state and continues it, reusing cached chapters. This assumes a **single
worker** — running more than one would need a lease or heartbeat so they don't
steal each other's jobs.

That check runs on every idle poll, not only at startup. Tying it to startup
left one way to strand a book permanently: lose the database mid-narration —
a server redeploy is enough — and the job stays marked running, while the
handler that would record the failure needs the very connection that just
died. Since `claim_job` only ever takes a `queued` row, nothing would touch it
again. Cancelling did not help either, because cancelling a running job sets a
flag for the owning worker to notice and there was no owner: the book sat at
*Stopping…* indefinitely. Now an idle narrator treats any job still marked
running as nobody's and re-queues it, and the API cancels outright when no
narrator has checked in, rather than waiting on one that will never answer.

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

Chapter audio is kept on the **narrator's** own disk, under
`~/Library/Application Support/Vocalis/work/<job-id>/`, and reused on any
later run of that job. So:

- A job that fails partway **resumes** instead of re-narrating from scratch.
- `POST /api/jobs/{id}/reassemble` ("Rebuild file" in the UI) rewrites the M4B
  from cached audio in seconds — use it after editing chapter titles or when
  only the ffmpeg step failed.

The cache costs roughly 170 MB per hour of audio, so finished books should be
cleared out once you have filed the M4B away:

- **Edit chapters** reopens a finished book's plan: rename sections, or untick
  one that shouldn't be there (a contents page narrated by mistake), then
  rebuild — `POST /api/jobs/{id}/reassemble` with the revised plan. The
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
describes only the run that happened to finish. One book interrupted six times
reported "converted in 60s" for what was really hours, because the last run was
just the assembly step. `work_seconds` is added to as chapters land, so
it survives crashes and resumes and a crash costs at most one chapter's worth of
tally. Books converted before the column existed show no figure at all rather
than a recovered guess; their playing time was backfilled from the M4B with
`ffprobe`, which is exact.

## Listening and reading along

A finished book can be played without leaving Vocalis, and read while it is
narrated.

- **Listen** opens a player: play, scrub, ±30s, and a chapter list you can jump
  around with. Chapter marks written into the M4B are invisible to a browser's
  `<audio>`, so these come from the timings recorded during narration
  (`GET /api/jobs/{id}/chapters`).
- **Read along** shows the book's own text — italics, headings, block quotes,
  not a flattened transcript — with the **word** being spoken lit up, and a
  chapter picker to move around. Clicking any word jumps the recording there.

Words are found by forced alignment: after a segment is narrated, the audio is
aligned against the text that produced it, which is a far easier problem than
recognition because the words are already known. Interpolating across the
sentence would have been cheaper and is not good enough — Chatterbox inserts
real pauses of its own, and one measured sentence carried 0.9s and 0.5s of
silence in the middle of it, so evenly spread timings would sit a second away
from the voice.

It runs on the CPU at roughly 25x realtime, so a nine-hour book costs about
twenty minutes on top of narration. It needs only audio and text, which is why
a book narrated before any of this existed can be given word timings by
rebuilding it rather than narrating it again. Paragraphs carrying inline markup
still highlight a sentence at a time: splitting an `<em>` that straddles a word
would either break the HTML or lose the emphasis.

Alignment is *verified*, not assumed. `GET /api/jobs/{id}/read` re-chunks each
chapter and compares against what was narrated; a chapter whose counts disagree
is shown as text with no highlighting rather than highlighting the wrong
sentence. Books narrated before timings were recorded show no **Read along**
button at all.

If you have such a book, its timings can usually be recovered without narrating
it again: chunks are joined with a pause of literal digital silence, so the
boundaries are still in the cached audio. Rebuild the book (**Edit chapters →
Rebuild**) and the narrator reads them back out of the audio it already has,
accepting a segment only when the pauses divide it into exactly as many pieces
as it had chunks.

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

- `docker-compose.yml` — the deployment stack (pulls published images);
  `docker-compose.build.yml` — override to build from this checkout
- `db/init.sql` — job-table schema, baked into the db image
- `core/` — **shared** EPUB parsing and text preparation. Installed into the
  API image and editable-installed into the worker venv (`uv pip install -e
  ../core`) so both halves agree on chapters, titles, and cover art.
- `api/` — FastAPI: analyze, review, start, status, download
- `worker/` — native pipeline: plan → clean → Chatterbox synth → ffmpeg M4B
- `web/` — React/TS UI (nginx serves it and proxies `/api` to the API)

## License

[AGPL-3.0](LICENSE) — you're free to use, modify, and self-host this software;
if you run a modified version as a network service, you must make your source
available to its users.

The pieces it builds on keep their own terms: Chatterbox (MIT), and the bundled
narrator clips, which were synthesized with Kokoro-82M (Apache-2.0). Both are
compatible with the AGPL.
