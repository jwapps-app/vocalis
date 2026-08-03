CREATE TYPE job_status AS ENUM (
  -- 'draft' is an analyzed-but-unconfirmed upload: the chapter plan is ready
  -- for review in the UI and the worker ignores it until it becomes 'queued'.
  'draft', 'queued', 'parsing', 'synthesizing', 'assembling', 'done', 'failed',
  -- 'cancelled' keeps its cached chapter audio, so it can be resumed later.
  'cancelled'
);

CREATE TABLE jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  status job_status NOT NULL DEFAULT 'queued',

  -- Paths are relative to the shared data dir (/data in the API container,
  -- VOCALIS_DATA_DIR for the native worker).
  epub_filename TEXT NOT NULL,
  epub_path TEXT NOT NULL,
  voice_ref_path TEXT,
  output_path TEXT,

  seed INTEGER NOT NULL DEFAULT 1234,
  narrator TEXT NOT NULL DEFAULT 'default',

  -- 'full' produces the M4B; 'sample' narrates ~a minute for auditioning.
  mode TEXT NOT NULL DEFAULT 'full',
  -- Samples point at the draft they were auditioned from, so they can be
  -- cleaned up when that draft is converted or deleted.
  parent_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
  -- Reviewed chapter plan: [{index, title, source, include, chars}, ...].
  -- Titles here override what the parser found; include=false skips a section.
  chapters JSONB,

  -- How many chapters to narrate at once. Each one costs its own copy of the
  -- model in memory; the audio produced is identical either way.
  concurrency INTEGER NOT NULL DEFAULT 2,
  -- Set by the cancel endpoint; the worker checks it between chunks.
  cancel_requested BOOLEAN NOT NULL DEFAULT false,
  -- Chatterbox generation params for the chosen narrator (exaggeration, cfg_weight).
  tts_params JSONB NOT NULL DEFAULT '{}',
  -- Drop bare in-text citations (scripture refs, "see p. 42", years) from the
  -- narration. A per-book choice: a study Bible wants them read, a devotional
  -- magazine does not. Prose in parentheses is always kept.
  drop_citations BOOLEAN NOT NULL DEFAULT false,

  title TEXT,
  author TEXT,
  chapter_count INTEGER,
  chapters_done INTEGER NOT NULL DEFAULT 0,
  progress REAL NOT NULL DEFAULT 0,
  estimated_total_seconds REAL,

  -- Narration time accumulated across every run of this job. started_at is
  -- reset each time the worker claims the job, so finished_at - started_at
  -- describes only the last run — for a book resumed after a crash that read
  -- as "converted in 60s" when the truth was hours. Added to as chapters land,
  -- so a crash costs at most one chapter's worth rather than the whole tally.
  work_seconds REAL NOT NULL DEFAULT 0,
  -- Playing time of the finished audiobook.
  audio_seconds REAL,
  -- Where every chapter and chunk of text falls in the finished recording:
  --   {"chapters": [{"index": 0, "title": "...", "start": 0.0, "end": 237.0}],
  --    "chunks":   [{"chapter": 0, "text": "...", "start": 0.0, "end": 4.2}]}
  --
  -- Recorded during synthesis because it cannot be recovered afterwards: the
  -- offsets are known while the audio is being concatenated and nowhere else,
  -- short of running speech recognition over the finished file. A book
  -- narrated without this would have to be narrated again to gain it.
  timings JSONB,

  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX jobs_status_created_idx ON jobs (status, created_at);

-- One row per running worker, touched on every poll. The API reads it to tell
-- the UI whether a narrator is connected and what hardware it found — without
-- this a missing worker is indistinguishable from a slow one, and a silent
-- CPU fallback (catastrophically slow) is invisible. A single-row-per-host
-- design; `id` is a stable per-machine token the worker generates once.
CREATE TABLE workers (
  id           TEXT PRIMARY KEY,
  hostname     TEXT,
  device       TEXT,        -- 'mps' | 'cuda' | 'cpu'
  device_name  TEXT,        -- e.g. 'Apple M4 Pro', 'NVIDIA RTX 4090'
  -- GPU left over by everything else running, measured at the last job start.
  -- Unified memory means this moves with the desktop, so it is shown in the UI
  -- rather than hidden: it is what decides how many chapters run at once.
  free_gpu_gb  REAL,
  max_concurrency INTEGER,  -- what that headroom actually allows
  version      TEXT,
  last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-row table holding the instance's own secrets.
--
-- Vocalis has one user, so there is no accounts table — but the password still
-- needs somewhere to live that is not the compose file. Keeping it here rather
-- than in an environment variable means it can be set from the browser on
-- first run, the way Portainer does it: no editing .env, no reading a
-- generated password out of container logs, and nothing secret sitting in a
-- file that gets pasted into a forum post when someone asks for help.
-- The secrets are filled in by the API on first start rather than here:
-- gen_random_bytes() lives in pgcrypto, and requiring an extension to stand up
-- a schema is a needless way for a fresh deployment to fail.
CREATE TABLE instance (
  id                 BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
  -- Nullable: instances created before usernames existed sign in on the
  -- password alone until one is chosen.
  username           TEXT,
  password_hash      TEXT,
  -- Signs browser sessions. Generated once; rotating it logs everyone out.
  secret_key         TEXT,
  -- Presented by the narrator instead of a login, since it fetches books with
  -- curl and cannot fill in a form. Travels in the worker bundle, which is
  -- itself behind the password.
  worker_token       TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
