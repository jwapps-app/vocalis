import { DragEvent, useEffect, useRef, useState } from "react";
import {
  analyzeEpub,
  cancelJob,
  deleteJob,
  resumeJob,
  downloadUrl,
  Job,
  listJobs,
  listNarrators,
  Narrator,
} from "./api";
import EditChapters from "./EditChapters";
import Player from "./Player";
import Reader from "./Reader";
import Login from "./Login";
import Review from "./Review";
import Setup from "./Setup";
import { InstallButton } from "./Install";
import { authStatus, getWorker, logout, Unauthorized, withTimeout, Worker } from "./api";

const ACTIVE = new Set(["queued", "parsing", "synthesizing", "assembling"]);

const STAGE_LABEL: Record<string, string> = {
  draft: "Needs review",
  queued: "Waiting for the narrator",
  parsing: "Reading the book",
  synthesizing: "Recording",
  assembling: "Adding chapters",
  done: "Ready",
  failed: "Failed",
  cancelled: "Stopped",
};

function humanSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  const mb = bytes / 1024 / 1024;
  return mb < 1 ? `${Math.round(bytes / 1024)} KB` : `${Math.round(mb)} MB`;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// The worker clears its estimate when it claims a job and only measures a new
// one once a chapter lands, so an active job with no estimate is genuinely
// still working it out — say so rather than showing a stale or absent figure.
function eta(job: Job): string | null {
  if (!ACTIVE.has(job.status)) return null;
  if (!job.estimated_total_seconds || !job.started_at) return "calculating…";
  const elapsed = (Date.now() - new Date(job.started_at).getTime()) / 1000;
  const remaining = job.estimated_total_seconds - elapsed;
  return remaining <= 0 ? "almost done" : `${duration(remaining)} left`;
}

/** Time actually spent narrating, summed over every run.
 *
 * Not finished_at - started_at: started_at is reset each time the worker
 * claims the job, so that measures only the run that happened to finish. A
 * book resumed after five interruptions reported "converted in 60s" — the
 * duration of the final assembly step.
 */
function elapsedTotal(job: Job): string | null {
  return job.work_seconds > 1 ? duration(job.work_seconds) : null;
}

/** Playing time of the finished audiobook, e.g. "3h 14m long". */
function audioLength(job: Job): string | null {
  return job.audio_seconds ? duration(job.audio_seconds) : null;
}

function JobCard({
  job,
  onRebuilt,
  onReview,
  onEditChapters,
  onListen,
  onRead,
}: {
  job: Job;
  onRebuilt: () => void;
  onReview: (job: Job) => void;
  onEditChapters: (job: Job) => void;
  onListen: (job: Job) => void;
  onRead: (job: Job) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const active = ACTIVE.has(job.status);
  const done = job.status === "done";
  const isDraft = job.status === "draft";

  const meta: string[] = isDraft
    ? [`${job.chapters?.filter((c) => c.include).length ?? 0} sections selected`]
    : [job.narrator];
  if (done) {
    // Listening time first — it is what you would want to know before pressing
    // play; how long it took to make is a footnote by comparison.
    const length = audioLength(job);
    if (length) meta.push(`${length} long`);
    if (job.chapter_count) meta.push(`${job.chapter_count} chapters`);
    const t = elapsedTotal(job);
    if (t) meta.push(`converted in ${t}`);
    // The audiobook itself — the number worth showing. Not the working cache,
    // which is scratch and, mid-run, dwarfs the finished file it becomes.
    if (job.output_bytes) meta.push(humanSize(job.output_bytes));
  }
  if (job.status === "cancelled" && job.chapter_count) {
    meta.push(`stopped after ${job.chapters_done} of ${job.chapter_count} chapters`);
  }

  async function remove() {
    try {
      await deleteJob(job.id);
      onRebuilt();
    } catch (err) {
      alert(`Could not delete: ${err instanceof Error ? err.message : err}`);
      setConfirming(false);
    }
  }

  async function stop() {
    try {
      await cancelJob(job.id);
      onRebuilt();
    } catch (err) {
      alert(`Could not cancel: ${err instanceof Error ? err.message : err}`);
    }
  }

  async function resume() {
    try {
      await resumeJob(job.id);
      onRebuilt();
    } catch (err) {
      alert(`Could not resume: ${err instanceof Error ? err.message : err}`);
    }
  }

  return (
    <li className={`job job-${job.status}`}>
      <div className="job-head">
        <h3 className="job-title">{job.title ?? job.epub_filename}</h3>
        <span className={`pill pill-${job.status}`}>
          {active && <span className="pulse" />}
          {STAGE_LABEL[job.status] ?? job.status}
        </span>
      </div>

      <p className="job-meta">{meta.join(" · ")}</p>

      {active && (
        <div className="progress">
          <div className="bar">
            <div className="bar-fill" style={{ width: `${Math.max(job.progress, 2)}%` }} />
          </div>
          <div className="progress-meta">
            <span>
              {job.chapter_count
                ? `Chapter ${job.chapters_done} of ${job.chapter_count}`
                : "Starting up"}
            </span>
            <span>{eta(job) ?? `${Math.round(job.progress)}%`}</span>
          </div>
        </div>
      )}

      {confirming ? (
        <div className="confirm">
          <span>
            Delete this book from Vocalis?{" "}
            {done && "Download the M4B first — it is removed too."}
          </span>
          <div className="job-actions">
            <button type="button" className="btn btn-danger btn-small" onClick={remove}>
              Delete {job.disk_bytes ? humanSize(job.disk_bytes) : ""}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => setConfirming(false)}
            >
              Keep
            </button>
          </div>
        </div>
      ) : active ? (
        <div className="job-actions">
          <button
            type="button"
            className="btn btn-ghost btn-small btn-quiet"
            onClick={stop}
            disabled={job.cancel_requested}
            title="Stop narrating — finished chapters are kept so you can resume"
          >
            {job.cancel_requested ? "Stopping…" : "Cancel"}
          </button>
        </div>
      ) : (
        <div className="job-actions">
          {isDraft && (
            <button type="button" className="btn btn-download" onClick={() => onReview(job)}>
              Review &amp; convert
            </button>
          )}
          {done && (
            <button type="button" className="btn btn-download" onClick={() => onListen(job)}>
              ▶ Listen
            </button>
          )}
          {done && job.has_timings && (
            <button
              type="button"
              className="btn btn-download"
              onClick={() => onRead(job)}
              title="Read along while it is narrated"
            >
              Read along
            </button>
          )}
          {done && (
            <a className="btn btn-ghost btn-small" href={downloadUrl(job)}>
              Download M4B
            </a>
          )}
          {(job.status === "cancelled" || job.status === "failed") && (
            <button
              type="button"
              className="btn btn-download"
              onClick={resume}
              title="Continue from where it stopped — narrated chapters are reused"
            >
              Resume
            </button>
          )}
          {done && (
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => onEditChapters(job)}
              title="Rename chapters and rewrite the file from the recording — no re-narration"
            >
              Edit chapters
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-small btn-quiet"
            onClick={() => setConfirming(true)}
          >
            Delete
          </button>
        </div>
      )}

      {job.status === "failed" && (
        <pre className="error-box">{job.error?.trimEnd().split("\n").slice(-3).join("\n")}</pre>
      )}
    </li>
  );
}

export default function App() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [narrators, setNarrators] = useState<Narrator[]>([]);
  const [draft, setDraft] = useState<Job | null>(null);
  const [editing, setEditing] = useState<Job | null>(null);
  const [playing, setPlaying] = useState<Job | null>(null);
  const [reading, setReading] = useState<Job | null>(null);
  const [analyzing, setAnalyzing] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  // The manifest's app shortcuts launch /?view=setup, so honour that here or
  // they would quietly open the default view instead.
  const [tab, setTab] = useState<"library" | "setup">(() =>
    new URLSearchParams(window.location.search).get("view") === "setup" ? "setup" : "library"
  );
  const [worker, setWorker] = useState<Worker | null>(null);
  const [requiredRevision, setRequiredRevision] = useState(1);
  const [workerLoaded, setWorkerLoaded] = useState(false);
  /* Where the app is before it will show anything.
   *
   *   loading — still asking the server
   *   setup   — no password exists yet; choose one
   *   login   — a password exists and this browser has no session
   *   ready   — go ahead
   *
   * A plain boolean could not express "setup", and that mattered: an
   * unconfigured server answers every request, so nothing ever returned 401
   * and the first-run screen was unreachable. A new install would run wide
   * open and never mention it. */
  const [gate, setGate] = useState<
    "loading" | "setup" | "login" | "ready" | "unreachable"
  >("loading");
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const tick = () =>
      getWorker()
        .then((r) => {
          setWorker(r.worker);
          setRequiredRevision(r.required_revision);
        })
        .catch(() => {})
        .finally(() => setWorkerLoaded(true));
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const tick = () => listNarrators().then(setNarrators).catch(() => {});
    tick();
    const id = setInterval(tick, 10000);
    return () => clearInterval(id);
  }, []);

  const refreshJobs = () =>
    listJobs().then(
      (j) => setJobs(j),
      (err) => {
        if (err instanceof Unauthorized) setGate("login");
      }
    );

  // Asked once at startup, and again after signing out: is there a password at
  // all, and does this browser hold a session?
  const checkGate = async () => {
    try {
      // Bounded: a request that never settles used to leave the app rendering
      // nothing at all — a blank page, with no error and nothing to retry.
      // A server that hangs is commoner than one that refuses.
      const { configured } = await withTimeout(authStatus(), 8000);
      if (!configured) return setGate("setup");
      try {
        await withTimeout(listJobs(), 8000);
        setGate("ready");
      } catch (err) {
        setGate(err instanceof Unauthorized ? "login" : "unreachable");
      }
    } catch {
      setGate("unreachable");
    }
  };

  useEffect(() => {
    checkGate();
  }, []);

  useEffect(() => {
    refreshJobs();
    const id = setInterval(refreshJobs, 2000);
    return () => clearInterval(id);
  }, []);

  async function analyze(file: File) {
    setMessage(null);
    setAnalyzing(file.name);
    try {
      setDraft(await analyzeEpub(file));
    } catch (err) {
      setMessage(`Could not read that book: ${err instanceof Error ? err.message : err}`);
    } finally {
      setAnalyzing(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = Array.from(e.dataTransfer.files).find((f) =>
      f.name.toLowerCase().endsWith(".epub")
    );
    if (file) analyze(file);
    else setMessage("That doesn't look like an EPUB file.");
  }

  const active = jobs.filter((j) => ACTIVE.has(j.status)).length;
  const waiting = jobs.filter((j) => j.status === "queued").length;
  const offline = workerLoaded && (!worker || !worker.online);
  // Only nag when it actually matters: work is queued and nothing is there to
  // narrate it. A green narrator or an empty queue needs no banner.
  const stalled = offline && waiting > 0;
  // A narrator older than the server still narrates — it just silently leaves
  // out whatever it was never taught to record, and the only visible trace is
  // a button that never appears. Say it plainly instead.
  const outdated =
    workerLoaded && worker !== null && worker.revision < requiredRevision;

  if (gate === "loading") {
    return (
      <div className="page">
        <p className="hint">Connecting…</p>
      </div>
    );
  }

  if (gate === "unreachable") {
    return (
      <div className="page">
        <header className="masthead">
          <img className="logo" src="/icon.svg" alt="" width="52" height="52" />
          <div>
            <h1>Vocalis</h1>
            <p className="tagline">EPUB to audiobook, narrated locally.</p>
          </div>
        </header>
        <section className="card">
          <h2>Can't reach the server</h2>
          <p className="hint">
            The page loaded but the API did not answer. The <code>api</code> container
            is probably down or still starting — its log will say which. Vocalis will
            not work until it responds.
          </p>
          <button type="button" className="btn btn-primary" onClick={() => {
            setGate("loading");
            checkGate();
          }}>
            Try again
          </button>
        </section>
      </div>
    );
  }

  if (gate === "setup" || gate === "login") {
    return (
      <div className="page">
        <header className="masthead">
          <img className="logo" src="/icon.svg" alt="" width="52" height="52" />
          <div>
            <h1>Vocalis</h1>
            <p className="tagline">EPUB to audiobook, narrated locally.</p>
          </div>
        </header>
        <Login onAuthenticated={() => { setGate("ready"); refreshJobs(); }} />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="masthead">
        <img className="logo" src="/icon.svg" alt="" width="52" height="52" />
        <div>
          <h1>Vocalis</h1>
          <p className="tagline">EPUB to audiobook, narrated locally on your Mac.</p>
        </div>
        <nav className="tabs">
          <InstallButton />
          <button
            className={`tab${tab === "library" ? " active" : ""}`}
            onClick={() => setTab("library")}
          >
            Library
          </button>
          <button
            className={`tab${tab === "setup" ? " active" : ""}`}
            onClick={() => setTab("setup")}
            title={worker?.online ? "Narrator connected" : "Narrator not connected"}
          >
            <span className={`tab-dot ${worker?.online ? "on" : "off"}`} />
            Setup
          </button>
          {gate === "ready" && (
            <button
              className="tab"
              onClick={async () => {
                await logout().catch(() => {});
                await checkGate();
              }}
              title="Sign out"
            >
              Sign out
            </button>
          )}
        </nav>
      </header>

      {outdated && tab === "library" && (
        <div className="notice warn">
          <strong>Your narrator is out of date.</strong> It will still record
          books, but without the read-along and chapter marks this version adds.{" "}
          <button className="linklike" onClick={() => setTab("setup")}>
            Reinstall it
          </button>{" "}
          to get them.
        </div>
      )}

      {stalled && tab === "library" && (
        <div className="notice warn">
          <strong>
            {waiting} book{waiting > 1 ? "s are" : " is"} waiting, but no narrator is
            connected.
          </strong>{" "}
          <button className="linklike" onClick={() => setTab("setup")}>
            Set up the narrator
          </button>{" "}
          to start recording.
        </div>
      )}

      {tab === "setup" ? (
        <Setup />
      ) : editing ? (
        <EditChapters
          job={editing}
          onSaved={() => {
            setEditing(null);
            refreshJobs();
          }}
          onCancel={() => setEditing(null)}
        />
      ) : draft ? (
        <Review
          draft={draft}
          narrators={narrators}
          maxConcurrency={worker?.max_concurrency ?? 4}
          onQueued={() => {
            setDraft(null);
            refreshJobs();
          }}
          onCancel={() => setDraft(null)}
        />
      ) : (
        <section className="card">
          <div className="field">
            <label
              htmlFor="epub"
              className={`dropzone${dragging ? " dragging" : ""}${analyzing ? " busy" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
            >
              {analyzing ? (
                <>
                  <strong>Reading {analyzing}…</strong>
                  <span className="hint">Finding chapters</span>
                </>
              ) : (
                <>
                  <strong>Drop an EPUB here</strong>
                  <span className="hint">
                    or click to choose — you'll review the chapters before anything is recorded
                  </span>
                </>
              )}
            </label>
            <input
              id="epub"
              ref={fileRef}
              type="file"
              accept=".epub"
              className="visually-hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) analyze(f);
              }}
            />
            {message && <p className="error">{message}</p>}
          </div>
        </section>
      )}

      {playing && <Player job={playing} onClose={() => setPlaying(null)} />}
      {reading && <Reader job={reading} onClose={() => setReading(null)} />}

      {tab === "library" && (
        <section className="jobs-section">
          <h2>
            Library
            {active > 0 && <span className="badge">{active} in progress</span>}
          </h2>
          {jobs.length === 0 ? (
            <p className="empty">Nothing converted yet. Drop a book above to begin.</p>
          ) : (
            <ul className="jobs">
              {jobs.map((j) => (
                <JobCard
                  key={j.id}
                  job={j}
                  onRebuilt={refreshJobs}
                  onReview={setDraft}
                  onEditChapters={setEditing}
                  onListen={setPlaying}
                  onRead={setReading}
                />
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
