import { useEffect, useRef, useState } from "react";
import CitationPreview from "./Citations";
import {
  ChapterPlan,
  Citations,
  coverUrl,
  Excerpt,
  getCitations,
  getExcerpts,
  getJob,
  Job,
  Narrator,
  previewUrl,
  startJob,
  TitleSource,
  uploadVoice,
} from "./api";

const SOURCE_NOTE: Record<Exclude<TitleSource, "toc">, string> = {
  heading: "Not in the contents — named from a heading in the text",
  derived: "Not in the contents — named from its opening words",
  generic: "Not in the contents and no heading found — please name it",
};

const SOURCE_LABEL: Record<Exclude<TitleSource, "toc">, string> = {
  heading: "from heading",
  derived: "guessed",
  generic: "unnamed",
};

function ChapterRow({
  chapter,
  excerpt,
  open,
  onToggle,
  onChange,
}: {
  chapter: ChapterPlan;
  excerpt: Excerpt | undefined;
  open: boolean;
  onToggle: () => void;
  onChange: (next: ChapterPlan) => void;
}) {
  const flagged = chapter.source !== "toc";
  return (
    <li className={`chapter${chapter.include ? "" : " excluded"}${open ? " open" : ""}`}>
      <div className="chapter-row">
        <input
          type="checkbox"
          checked={chapter.include}
          aria-label={`Include ${chapter.title}`}
          onChange={(e) => onChange({ ...chapter, include: e.target.checked })}
        />
        <div className="chapter-main">
          <input
            className={`chapter-title${flagged ? " flagged" : ""}`}
            value={chapter.title}
            onChange={(e) => onChange({ ...chapter, title: e.target.value })}
          />
          {flagged && (
            <span className={`tag tag-${chapter.source}`} title={SOURCE_NOTE[chapter.source as Exclude<TitleSource, "toc">]}>
              {SOURCE_LABEL[chapter.source as Exclude<TitleSource, "toc">]}
            </span>
          )}
        </div>
        <span className="chapter-size">{(chapter.chars / 1000).toFixed(1)}k</span>
        <button
          type="button"
          className="chapter-peek"
          aria-expanded={open}
          title={open ? "Hide the opening words" : "Read the opening words"}
          onClick={onToggle}
        >
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          <span className="visually-hidden">Preview {chapter.title}</span>
        </button>
      </div>

      {open && (
        <div className="chapter-excerpt">
          {excerpt ? (
            <>
              <p>
                {excerpt.excerpt}
                {excerpt.truncated && <span className="chapter-more">…</span>}
              </p>
              <p className="hint">
                {excerpt.chars.toLocaleString()} characters in total
                {excerpt.truncated ? " — showing the opening" : ""}
              </p>
            </>
          ) : (
            <p className="hint">Loading…</p>
          )}
        </div>
      )}
    </li>
  );
}

export default function Review({
  draft,
  narrators,
  maxConcurrency,
  onQueued,
  onCancel,
}: {
  draft: Job;
  narrators: Narrator[];
  maxConcurrency: number;
  onQueued: () => void;
  onCancel: () => void;
}) {
  const [plan, setPlan] = useState<ChapterPlan[]>(draft.chapters ?? []);
  const [narrator, setNarrator] = useState(narrators[0]?.id ?? "");
  // Never offer more than the connected narrator can actually run.
  const cap = Math.max(1, maxConcurrency);
  const [concurrency, setConcurrency] = useState(Math.min(draft.concurrency || 2, cap));
  const [dropCitations, setDropCitations] = useState(false);
  const [excerpts, setExcerpts] = useState<Map<number, Excerpt>>(new Map());
  const [openChapter, setOpenChapter] = useState<number | null>(null);
  const [voiceFile, setVoiceFile] = useState<File | null>(null);
  const [busy, setBusy] = useState<null | "sample" | "full">(null);
  const [sampleJob, setSampleJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [hasCover, setHasCover] = useState(true);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (narrators.length && !narrators.some((n) => n.id === narrator) && narrator !== "custom") {
      setNarrator(narrators[0].id);
    }
  }, [narrators]);

  const selected = narrators.find((n) => n.id === narrator);
  const included = plan.filter((c) => c.include);
  const flagged = plan.filter((c) => c.source !== "toc" && c.include);
  const words = Math.round(included.reduce((n, c) => n + c.chars, 0) / 5.5 / 1000);

  function setChapter(next: ChapterPlan) {
    setPlan((p) => p.map((c) => (c.index === next.index ? next : c)));
  }

  function stopAudio() {
    audioRef.current?.pause();
    audioRef.current = null;
    setPlaying(false);
  }

  function play(url: string) {
    stopAudio();
    const audio = new Audio(url);
    audio.onended = audio.onerror = stopAudio;
    audioRef.current = audio;
    setPlaying(true);
    audio.play().catch(stopAudio);
  }

  useEffect(() => stopAudio, []);

  // One fetch covers every chapter, so expanding a row is instant. Refetched
  // when the citation setting changes, since the excerpt is meant to show what
  // will actually be read aloud.
  useEffect(() => {
    let live = true;
    getExcerpts(draft.id, dropCitations).then(
      (r) => live && setExcerpts(new Map(r.excerpts.map((e) => [e.index, e]))),
      () => {}
    );
    return () => {
      live = false;
    };
  }, [draft.id, dropCitations]);

  async function resolveVoice(): Promise<string | null> {
    if (narrator !== "custom") return null;
    if (!voiceFile) throw new Error("choose a reference voice clip");
    return (await uploadVoice(draft.id, voiceFile)).voice_ref_path;
  }

  async function makeSample() {
    setBusy("sample");
    setError(null);
    setSampleJob(null);
    try {
      const voice_ref_path = await resolveVoice();
      let job = await startJob(draft.id, {
        narrator,
        chapters: plan,
        mode: "sample",
        voice_ref_path,
        drop_citations: dropCitations,
      });
      // Short poll — a sample is a handful of chunks, under a minute of work.
      while (job.status !== "done" && job.status !== "failed") {
        await new Promise((r) => setTimeout(r, 2000));
        job = await getJob(job.id);
        setSampleJob(job);
      }
      setSampleJob(job);
      if (job.status === "done") play(`/api/jobs/${job.id}/download`);
      else setError("The sample failed to render.");
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(null);
    }
  }

  async function convert() {
    setBusy("full");
    setError(null);
    try {
      const voice_ref_path = await resolveVoice();
      await startJob(draft.id, {
        narrator,
        chapters: plan,
        mode: "full",
        concurrency,
        voice_ref_path,
        drop_citations: dropCitations,
      });
      stopAudio();
      onQueued();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setBusy(null);
    }
  }

  return (
    <section className="card review">
      <div className="review-head">
        {hasCover && (
          <img
            className="cover"
            src={coverUrl(draft.id)}
            alt=""
            onError={() => setHasCover(false)}
          />
        )}
        <div>
          <h2 className="book-title">{draft.title}</h2>
          <p className="hint">{draft.author}</p>
          <p className="hint">
            {included.length} of {plan.length} sections · about {words}k words
          </p>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {flagged.length > 0 && (
        <div className="notice">
          <strong>
            {flagged.length} section{flagged.length > 1 ? "s aren't" : " isn't"} in the book's
            table of contents.
          </strong>{" "}
          Their names were worked out from the text — check them below and edit if needed.
        </div>
      )}

      <div className="field">
        <span className="field-label">Chapters</span>
        <ul className="chapters">
          {plan.map((c) => (
            <ChapterRow
              key={c.index}
              chapter={c}
              excerpt={excerpts.get(c.index)}
              open={openChapter === c.index}
              onToggle={() => setOpenChapter((i) => (i === c.index ? null : c.index))}
              onChange={setChapter}
            />
          ))}
        </ul>
        <p className="hint">
          Unticked sections are skipped — front matter like the contents page is excluded
          automatically. Use ▸ to read a section's opening words if its title doesn't say
          enough.
        </p>
      </div>

      <div className="field">
        <label className="check">
          <input
            type="checkbox"
            checked={dropCitations}
            onChange={(e) => setDropCitations(e.target.checked)}
          />
          <span>
            <strong>Skip inline references</strong>
            <span className="hint">
              Leave out bracketed citations — scripture references, “see p. 42”, dates —
              that interrupt the listening and are the kind of thing the voice reads
              poorly. Words written in parentheses are still read. Best for devotionals
              and prose; leave off for study Bibles and commentaries where the references
              matter.
            </span>
          </span>
        </label>
        <CitationPreview
          jobId={draft.id}
          included={new Set(included.map((c) => c.index))}
        />
      </div>

      <div className="field">
        <span className="field-label">Narrator</span>
        <div className="narrator-row">
          <select value={narrator} onChange={(e) => setNarrator(e.target.value)}>
            {narrators.map((n) => (
              <option key={n.id} value={n.id}>
                {n.description ? `${n.name} — ${n.description}` : n.name}
              </option>
            ))}
            <option value="custom">Custom voice clip…</option>
          </select>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={!selected?.has_preview || playing}
            onClick={() => play(previewUrl(narrator))}
          >
            ▶ Preview
          </button>
        </div>
        {narrator === "custom" && (
          <input
            type="file"
            accept="audio/*"
            onChange={(e) => setVoiceFile(e.target.files?.[0] ?? null)}
          />
        )}
      </div>

      {cap > 1 && (
        <div className="field">
          <span className="field-label">Chapters at once</span>
          <div className="speed-row">
            {Array.from({ length: cap }, (_, i) => i + 1).map((n) => (
              <button
                key={n}
                type="button"
                className={`speed${concurrency === n ? " chosen" : ""}`}
                onClick={() => setConcurrency(n)}
              >
                <strong>{n}×</strong>
                <span>{n === 1 ? "slowest" : `~${n * 0.85}× faster`}</span>
              </button>
            ))}
          </div>
          <p className="hint">
            Narrating chapters side by side uses the graphics chip more fully, since one
            chapter at a time leaves most of it waiting. Each runs in its own process
            holding its own copy of the voice model, and a chapter needs several
            gigabytes to render — so this list only offers as many as the connected
            narrator's memory can actually hold at once. Gains taper off once the chip is
            saturated anyway. The audiobook is identical whichever you pick.
          </p>
        </div>
      )}

      <div className="actions">
        <button
          type="button"
          className="btn btn-ghost"
          onClick={busy === "sample" ? undefined : makeSample}
          disabled={busy !== null || !included.length}
        >
          {busy === "sample"
            ? sampleJob?.status === "synthesizing"
              ? "Recording sample…"
              : "Preparing sample…"
            : "▶ Hear a sample of this book"}
        </button>
        <button
          type="button"
          className="btn btn-primary"
          onClick={convert}
          disabled={busy !== null || !included.length}
        >
          {busy === "full" ? "Queueing…" : `Convert ${included.length} sections`}
        </button>
      </div>

      {sampleJob?.status === "done" && (
        <p className="hint">
          Sample ready.{" "}
          <button type="button" className="linklike" onClick={() => play(`/api/jobs/${sampleJob.id}/download`)}>
            Play it again
          </button>{" "}
          — if the voice isn't right, pick another and sample again.
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
