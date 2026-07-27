import { useEffect, useState } from "react";
import { Citations, getCitations } from "./api";

/**
 * How many references "Skip inline references" would remove, and what they
 * look like in place.
 *
 * The counts come from the same function that does the removing, so the number
 * is what will actually happen rather than an estimate. Counts are held per
 * chapter and totalled against the current selection, so unticking a chapter
 * updates the figure immediately.
 */
export default function CitationPreview({
  jobId,
  included,
}: {
  jobId: string;
  included: Set<number>;
}) {
  const [data, setData] = useState<Citations | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState(0);

  useEffect(() => {
    let live = true;
    getCitations(jobId).then(
      (d) => live && setData(d),
      () => live && setFailed(true)
    );
    return () => {
      live = false;
    };
  }, [jobId]);

  // The checkbox works with or without this; a failed count is not worth an
  // error message on a screen the user is already reading closely.
  if (failed) return null;
  if (!data) return <p className="hint">Counting references…</p>;

  const total = Object.entries(data.counts)
    .filter(([index]) => included.has(Number(index)))
    .reduce((sum, [, count]) => sum + count, 0);

  if (total === 0) {
    return <p className="hint">No inline references found in the chapters you've selected.</p>;
  }

  const examples = data.items.filter((c) => included.has(c.chapter));
  const shown = examples[Math.min(at, examples.length - 1)];
  const step = (delta: number) =>
    setAt((i) => (i + delta + examples.length) % examples.length);

  return (
    <div className="citations">
      <p className="hint">
        <strong>
          {total} reference{total === 1 ? "" : "s"}
        </strong>{" "}
        would be skipped
        {data.truncated && examples.length < total ? ` (${examples.length} shown)` : ""}.{" "}
        {examples.length > 0 && (
          <button
            type="button"
            className="linklike"
            onClick={() => {
              setOpen((o) => !o);
              setAt(0);
            }}
          >
            {open ? "Hide examples" : "See examples"}
          </button>
        )}
      </p>

      {open && shown && (
        <div className="citation-viewer">
          <p className="citation-quote">
            <span className="citation-context">…{shown.before}</span>
            <mark>{shown.text}</mark>
            <span className="citation-context">{shown.after}…</span>
          </p>
          <div className="citation-nav">
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => step(-1)}
              aria-label="Previous reference"
            >
              ‹
            </button>
            <span className="hint">
              {Math.min(at, examples.length - 1) + 1} of {examples.length}
              {shown.title ? ` · ${shown.title}` : ""}
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => step(1)}
              aria-label="Next reference"
            >
              ›
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
