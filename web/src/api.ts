export type JobStatus =
  | "draft"
  | "queued"
  | "parsing"
  | "synthesizing"
  | "assembling"
  | "done"
  | "failed"
  | "cancelled";

/** Where a chapter title came from. Anything but "toc" deserves a look. */
export type TitleSource = "toc" | "heading" | "derived" | "generic";

export interface ChapterPlan {
  index: number;
  title: string;
  source: TitleSource;
  include: boolean;
  chars: number;
}

export interface Narrator {
  id: string;
  name: string;
  description: string;
  has_preview: boolean;
}

export interface Job {
  id: string;
  status: JobStatus;
  mode: "full" | "sample";
  epub_filename: string;
  title: string | null;
  author: string | null;
  seed: number;
  narrator: string;
  chapters: ChapterPlan[] | null;
  concurrency: number;
  cancel_requested: boolean;
  chapter_count: number | null;
  chapters_done: number;
  progress: number;
  estimated_total_seconds: number | null;
  /** Narration time accumulated across every run, not just the last one. */
  work_seconds: number;
  /** Playing time of the finished audiobook. */
  audio_seconds: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  has_output: boolean;
  /** Upload + cached audio + output on disk. Only present in the list. */
  disk_bytes?: number;
  /** The finished M4B alone — the file you keep. List only. */
  output_bytes?: number;
}

export interface Worker {
  hostname: string | null;
  device: "mps" | "cuda" | "cpu" | "unknown" | null;
  device_name: string | null;
  /** GPU left free by everything else, measured at the last job start. */
  free_gpu_gb: number | null;
  max_concurrency: number | null;
  version: string | null;
  last_seen: string;
  online: boolean;
}

/** Raised when the server rejects the session, so the app can show the login
 *  screen instead of a generic error on every panel.
 *
 *  The session itself is an HttpOnly cookie the browser sends on its own —
 *  including on <img>, <audio> and download links, which cannot carry a header.
 *  Nothing is stored here. */
export class Unauthorized extends Error {}

/** Reject a promise that takes too long.
 *
 *  fetch has no default timeout, so a server that accepts the connection and
 *  then never replies leaves the caller waiting forever. That is exactly what
 *  a half-started container does, and it is what turned an unhealthy API into
 *  a blank page rather than a message. */
export function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((_, reject) =>
      setTimeout(() => reject(new Error("The server did not respond.")), ms)
    ),
  ]);
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (res.status === 401) throw new Unauthorized("Not authenticated");
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const authStatus = () => req<{ configured: boolean }>("/api/auth/status");

async function authenticate(path: string, password: string): Promise<void> {
  await req<{ ok: boolean }>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
}

export const logout = () => req<{ ok: boolean }>("/api/auth/logout", { method: "POST" });

export const login = (password: string) => authenticate("/api/auth/login", password);
export const setupPassword = (password: string) =>
  authenticate("/api/auth/setup", password);

export const previewUrl = (id: string) => `/api/narrators/${id}/preview`;
export const coverUrl = (jobId: string) => `/api/jobs/${jobId}/cover`;
export const downloadUrl = (job: Job) => `/api/jobs/${job.id}/download`;

/** One parenthetical that "Skip inline references" would remove. */
export interface Citation {
  text: string;
  before: string;
  after: string;
  chapter: number;
  title: string;
}

export interface Citations {
  /** Exact count per chapter index — always complete, even if items is capped. */
  counts: Record<string, number>;
  items: Citation[];
  truncated: boolean;
}

export const getCitations = (jobId: string) =>
  req<Citations>(`/api/jobs/${jobId}/citations`);

/** Opening words of a section, cleaned exactly as the narrator will read it. */
export interface Excerpt {
  index: number;
  chars: number;
  excerpt: string;
  truncated: boolean;
}

export const getExcerpts = (jobId: string, dropCitations: boolean) =>
  req<{ excerpts: Excerpt[] }>(
    `/api/jobs/${jobId}/excerpts?drop_citations=${dropCitations}`
  );

/** Chapter indexes that already have audio on disk — cheap to keep, cheap to
 *  drop. Anything else has to be narrated. */
export const getRecorded = (jobId: string) =>
  req<{ indexes: number[] }>(`/api/jobs/${jobId}/recorded`);

export const bundleUrl = "/api/worker/bundle";
export const getWorker = () =>
  req<{ worker: Worker | null; install_command: string }>("/api/worker");

export const listNarrators = () => req<Narrator[]>("/api/narrators");
export const listJobs = () => req<Job[]>("/api/jobs");
export const getJob = (id: string) => req<Job>(`/api/jobs/${id}`);

export function analyzeEpub(epub: File): Promise<Job> {
  const form = new FormData();
  form.append("epub", epub);
  return req<Job>("/api/jobs/analyze", { method: "POST", body: form });
}

export function uploadVoice(jobId: string, voice: File): Promise<{ voice_ref_path: string }> {
  const form = new FormData();
  form.append("voice", voice);
  return req(`/api/jobs/${jobId}/voice`, { method: "POST", body: form });
}

export function startJob(
  jobId: string,
  body: {
    narrator: string;
    chapters: ChapterPlan[];
    mode?: "full" | "sample";
    seed?: number;
    concurrency?: number;
    voice_ref_path?: string | null;
    drop_citations?: boolean;
  }
): Promise<Job> {
  return req<Job>(`/api/jobs/${jobId}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteJob(jobId: string): Promise<void> {
  const res = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    let detail = `${res.status}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* no body */
    }
    throw new Error(detail);
  }
}

export const cancelJob = (id: string) =>
  req<Job>(`/api/jobs/${id}/cancel`, { method: "POST" });

export const resumeJob = (id: string) =>
  req<Job>(`/api/jobs/${id}/resume`, { method: "POST" });

export function reassemble(jobId: string, chapters?: ChapterPlan[]): Promise<Job> {
  return req<Job>(`/api/jobs/${jobId}/reassemble`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(chapters ?? null),
  });
}
