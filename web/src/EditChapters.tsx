import { useEffect, useState } from "react";
import { ChapterPlan, getRecorded, Job, reassemble } from "./api";

/**
 * Change which sections are in a finished book, and what they are called, then
 * rewrite the M4B.
 *
 * Two very different costs hide behind the same tick-box, so the screen names
 * them. Dropping a section — a contents page that got narrated by mistake — is
 * only a repackage: the audio stays on disk and ffmpeg restamps the chapter
 * marks in seconds. Adding a section that was skipped the first time means
 * recording it, which is minutes to hours. Nothing here should be able to start
 * that silently.
 */
export default function EditChapters({
  job,
  onSaved,
  onCancel,
}: {
  job: Job;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [plan, setPlan] = useState<ChapterPlan[]>(job.chapters ?? []);
  const [recorded, setRecorded] = useState<Set<number> | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getRecorded(job.id).then(
      (r) => live && setRecorded(new Set(r.indexes)),
      () => live && setRecorded(new Set())
    );
    return () => {
      live = false;
    };
  }, [job.id]);

  const original = job.chapters ?? [];
  const wasIncluded = (index: number) =>
    original.find((c) => c.index === index)?.include ?? false;

  const included = plan.filter((c) => c.include);
  // Ticked now, but never recorded — these are what turn a rebuild into a
  // narration job.
  const needsNarration = recorded
    ? included.filter((c) => !recorded.has(c.index))
    : [];
  const dropped = plan.filter((c) => !c.include && wasIncluded(c.index));

  function update(index: number, patch: Partial<ChapterPlan>) {
    setPlan((p) => p.map((c) => (c.index === index ? { ...c, ...patch } : c)));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      // The whole plan goes back, including the unticked rows: the worker reads
      // a chapter index missing from the plan as *included*, so sending only
      // the ticked ones would restore everything this screen just removed.
      await reassemble(job.id, plan);
      onSaved();
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setSaving(false);
    }
  }

  const untitled = included.some((c) => !c.title.trim());

  return (
    <section className="card review">
      <div className="review-head">
        <div>
          <h2 className="book-title">{job.title ?? job.epub_filename}</h2>
          <p className="hint">
            Untick anything that shouldn't be in the audiobook, and rename what's
            left. The recording is reused, so rebuilding takes seconds.
          </p>
        </div>
        <button type="button" className="btn btn-ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>

      {needsNarration.length > 0 && (
        <div className="notice warn">
          <strong>
            {needsNarration.length} section
            {needsNarration.length > 1 ? "s were" : " was"} never recorded.
          </strong>{" "}
          Including {needsNarration.length > 1 ? "them" : "it"} means narrating
          {needsNarration.length > 1 ? " those sections" : " it"} now — minutes
          to hours, not seconds. Everything already recorded is reused.
        </div>
      )}

      <div className="field">
        <span className="field-label">Sections</span>
        <ul className="chapters">
          {plan.map((chapter) => {
            const isRecorded = recorded?.has(chapter.index) ?? true;
            return (
              <li
                key={chapter.index}
                className={`chapter${chapter.include ? "" : " excluded"}`}
              >
                <div className="chapter-row">
                  <input
                    type="checkbox"
                    checked={chapter.include}
                    aria-label={`Include ${chapter.title}`}
                    onChange={(e) =>
                      update(chapter.index, { include: e.target.checked })
                    }
                  />
                  <div className="chapter-main">
                    <input
                      className="chapter-title"
                      value={chapter.title}
                      aria-label={`Title of ${chapter.title}`}
                      onChange={(e) =>
                        update(chapter.index, { title: e.target.value })
                      }
                    />
                    {!isRecorded && (
                      <span
                        className="tag tag-generic"
                        title="No audio on disk — including this means recording it"
                      >
                        not recorded
                      </span>
                    )}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
        <p className="hint">
          {included.length} of {plan.length} sections in the audiobook
          {dropped.length > 0 && ` · ${dropped.length} being removed`}.
        </p>
      </div>

      <div className="actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={save}
          disabled={saving || untitled || included.length === 0}
        >
          {saving
            ? "Rebuilding…"
            : needsNarration.length > 0
            ? "Save and record the missing sections"
            : "Save and rebuild file"}
        </button>
      </div>

      {included.length === 0 && (
        <p className="hint">Keep at least one section.</p>
      )}
      {untitled && <p className="hint">Every included section needs a name.</p>}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
