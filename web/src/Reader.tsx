import { useEffect, useMemo, useRef, useState } from "react";
import { ChapterHead, downloadUrl, getReadable, getReadChapter, Job,
         ReadChapter } from "./api";
import { rememberRate, seekWhenReady, SPEEDS, storedRate } from "./media";

/**
 * Read the book while it is narrated, with the sentence being spoken lit up.
 *
 * The text is the book's own markup — italics, headings, verse — not a
 * flattened transcript. Each block carries the moments its sentences are read,
 * captured during synthesis, so following the audio is a lookup rather than a
 * guess.
 */
/* How far either side of a word the glow reaches, in seconds. Wide enough
   that consecutive words overlap — which is what makes it read as one band
   sliding along the line rather than a box hopping from word to word — and
   narrow enough that it never lights half a sentence. */
const BAND = 0.22;

type Span = { start: number; end: number; blockKey: string; chunkIndex: number };

export default function Reader({ job, onClose }: { job: Job; onClose: () => void }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const body = useRef<HTMLDivElement | null>(null);
  const [chapters, setChapters] = useState<ChapterHead[] | null>(null);
  const [chapter, setChapter] = useState<ReadChapter | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
  const [rate, setRate] = useState(storedRate);
  const [follow, setFollow] = useState(true);

  useEffect(() => {
    getReadable(job.id).then(
      (r) => setChapters(r.chapters),
      (err) => setError(String(err instanceof Error ? err.message : err))
    );
  }, [job.id]);

  useEffect(() => {
    const el = new Audio(downloadUrl(job));
    el.preload = "metadata";
    el.playbackRate = rate;
    audio.current = el;
    const onTime = () => setAt(el.currentTime);
    const onEnd = () => setPlaying(false);
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("ended", onEnd);
    return () => {
      el.pause();
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("ended", onEnd);
      audio.current = null;
    };
  }, [job.id]);

  /* One flat, ordered list of spoken moments. Built once: a book runs to
     thousands of chunks, and rebuilding this on every timeupdate — four times a
     second — would be the only expensive thing the reader does. */
  const spans = useMemo<Span[]>(() => {
    const out: Span[] = [];
    chapter?.blocks.forEach((b, bi) =>
      b.chunks.forEach((c, idx) =>
        out.push({ start: c.start, end: c.end, blockKey: `b${bi}`, chunkIndex: idx })
      )
    );
    return out;
  }, [chapter]);

  /* Binary search rather than a scan: at four updates a second over thousands
     of chunks, a linear search is wasted work on every tick. */
  const current = useMemo(() => {
    let lo = 0, hi = spans.length - 1, found: Span | null = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (at < spans[mid].start) hi = mid - 1;
      else if (at > spans[mid].end) lo = mid + 1;
      else { found = spans[mid]; break; }
    }
    return found;
  }, [spans, at]);

  /* Light the words continuously, outside React.
   *
   * Two reasons this is not state. `timeupdate` fires about four times a
   * second, so anything driven by it steps rather than moves — the very thing
   * a gliding highlight is meant to avoid. And re-rendering the page sixty
   * times a second to move one highlight would be absurd for a chapter of
   * several thousand words.
   *
   * So a frame loop reads the clock itself and writes two custom properties
   * on the words near the voice. Only the sentence being spoken is touched —
   * a few dozen elements — and everything else on the page is left alone.
   */
  const cursor = useRef<HTMLDivElement | null>(null);

  /* Every word in the book, in the order it is spoken, with where to find it
     on the page. Flat and ordered so the frame loop can binary search it. */
  type Stop = { at: number; key: string; index: number };
  const timeline = useMemo<Stop[]>(() => {
    const out: Stop[] = [];
    chapter?.blocks.forEach((b, bi) =>
      b.words.forEach((w, index) => out.push({ at: w[0], key: `b${bi}`, index }))
    );
    return out;
  }, [chapter]);

  useEffect(() => {
    const pane = body.current;
    const pill = cursor.current;
    if (!pane || !pill || timeline.length < 2) return;
    let frame = 0;
    let painted = -1;
    let cachedAt = -1;
    let here: HTMLElement | null = null;
    let next: HTMLElement | null = null;

    const find = (stop: Stop) =>
      pane.querySelector<HTMLElement>(
        `[data-key="${stop.key}"] [data-w="${stop.index}"]`
      );

    const paint = () => {
      frame = requestAnimationFrame(paint);
      const el = audio.current;
      if (!el) return;
      const t = el.currentTime;
      if (t === painted) return;        // paused, and nothing has moved
      painted = t;

      // The last word begun, and the one after it.
      let lo = 0, hi = timeline.length - 1, k = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (timeline[mid].at <= t) { k = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (k < 0 || k >= timeline.length - 1) {
        pill.style.opacity = "0";
        return;
      }

      if (k !== cachedAt) {
        here = find(timeline[k]);
        next = find(timeline[k + 1]);
        cachedAt = k;
      }
      if (!here) { pill.style.opacity = "0"; return; }

      /* Measured start-to-start, not across the word itself.
       *
       * A word's own duration ends where the silence begins, so interpolating
       * over it means arriving early and then waiting — and the narrator's
       * pauses are long enough to see, nearly a second inside one sentence.
       * Spanning the whole interval instead means the highlight is always in
       * motion, drifting through a pause rather than stopping dead in it. */
      const span = timeline[k + 1].at - timeline[k].at;
      const f = span > 0.01 ? Math.min(1, Math.max(0, (t - timeline[k].at) / span)) : 1;

      let left = here.offsetLeft;
      let top = here.offsetTop;
      let width = here.offsetWidth;

      let visible = 1;
      if (next) {
        if (next.offsetTop === top) {
          left += (next.offsetLeft - left) * f;
          width += (next.offsetWidth - width) * f;
        } else {
          /* A line break, where the two words have no path between them.
           *
           * Holding on one until the voice reaches the other is what made this
           * stop dead — nearly eight hundred milliseconds of stillness at the
           * end of every line. So it carries on off the end of the line it is
           * on, fades through the turn, and comes back in from the left of the
           * next one: the eye's own movement, and never actually stationary.
           */
          if (f < 0.45) {
            const run = f / 0.45;
            left += (here.offsetLeft + here.offsetWidth + 24 - left) * run;
            visible = 1 - run * 0.85;
          } else {
            const run = Math.min(1, (f - 0.55) / 0.45);
            top = next.offsetTop;
            width = next.offsetWidth;
            left = Math.max(0, next.offsetLeft - 24) +
                   (next.offsetLeft - Math.max(0, next.offsetLeft - 24)) * run;
            visible = 0.15 + run * 0.85;
          }
        }
      }

      pill.style.opacity = String(visible);
      pill.style.transform = `translate(${left}px, ${top}px)`;
      pill.style.width = `${width}px`;
      pill.style.height = `${here.offsetHeight}px`;
    };

    frame = requestAnimationFrame(paint);
    return () => {
      cancelAnimationFrame(frame);
      pill.style.opacity = "0";
    };
  }, [timeline]);

  useEffect(() => {
    if (!follow || !current || !body.current) return;
    // Fall back to the block: a paragraph rendered as the book's own markup
    // has no per-sentence elements to scroll to.
    const block = body.current.querySelector<HTMLElement>(
      `[data-key="${current.blockKey}"]`
    );
    const node =
      block?.querySelector<HTMLElement>(`[data-chunk="${current.chunkIndex}"]`) ?? block;
    node?.scrollIntoView({ block: "center", behavior: "smooth" });
    // Deliberately keyed on the sentence, not the word: scrolling on every
    // word would keep the page in constant motion while reading.
  }, [current, follow]);


  // Applied to the element that already exists, not only at creation, so
  // changing it mid-sentence takes effect on the words being spoken.
  useEffect(() => {
    if (audio.current) audio.current.playbackRate = rate;
    rememberRate(rate);
  }, [rate]);

  function toggle() {
    const el = audio.current;
    if (!el) return;
    if (el.paused) { el.play(); setPlaying(true); } else { el.pause(); setPlaying(false); }
  }

  function jumpTo(seconds: number) {
    const el = audio.current;
    if (!el) return;
    seekWhenReady(el, seconds);
    setAt(seconds);
    if (el.paused) { el.play(); setPlaying(true); }
  }

  /* The chapter being read. Chapters are in order, so the last one that has
     already started is the one we are in. */
  const chapterIndex = useMemo(() => {
    if (!chapters?.length) return -1;
    for (let i = chapters.length - 1; i >= 0; i--) {
      if (at >= chapters[i].start) return i;
    }
    return 0;
  }, [chapters, at]);

  /* Fetch whichever chapter the voice is in, and only that one.
   *
   * The whole book was too much to hold at once: wrapping every spoken word in
   * its own element turned one book's page into eighty-eight thousand of them,
   * which a phone will not lay out. A chapter is a few thousand at most, and
   * the reader only ever shows one. */
  useEffect(() => {
    if (!chapters?.length || chapterIndex < 0) return;
    if (chapter?.index === chapterIndex) return;
    let stale = false;
    setLoading(true);
    getReadChapter(job.id, chapterIndex).then(
      (c) => { if (!stale) { setChapter(c); setLoading(false); } },
      (err) => {
        if (stale) return;
        setLoading(false);
        setError(String(err instanceof Error ? err.message : err));
      }
    );
    return () => { stale = true; };
  }, [job.id, chapters, chapterIndex, chapter?.index]);

  /* Move the page as well as the audio.
   *
   * The whole book is one scroll, so jumping the recording without jumping the
   * text would leave the reader looking at wherever they had scrolled to. The
   * scroll is unconditional rather than left to the follow effect, which only
   * runs when Follow is ticked — turning it off should stop the page drifting
   * on its own, not stop it obeying a chapter you chose. */
  function goToChapter(index: number) {
    const target = chapters?.[index];
    const el = audio.current;
    if (!target || !el) return;
    // Unlike clicking a sentence, this does not start playing. Moving about
    // the book is as often reading as it is listening, and a paused reader who
    // picks a chapter has not asked for sound.
    seekWhenReady(el, target.start);
    setAt(target.start);
    // Only one chapter is on the page, so there is nothing to scroll *to* —
    // the effect above swaps it in, and the reader starts at the top of it.
    if (body.current) body.current.scrollTop = 0;
  }

  // Back within a few seconds of the start means the previous chapter; further
  // in, it restarts this one. As on the player, and on everything else.
  function stepChapter(delta: number) {
    if (chapterIndex < 0 || !chapters) return;
    const atStart = at - chapters[chapterIndex].start < 3;
    goToChapter(
      delta < 0 && !atStart
        ? chapterIndex
        : Math.min(chapters.length - 1, Math.max(0, chapterIndex + delta))
    );
  }

  if (error) {
    return (
      <div className="reader-overlay" onClick={onClose}>
        <section className="card reader" onClick={(e) => e.stopPropagation()}>
          <div className="reader-bar">
            <strong>{job.title ?? job.epub_filename}</strong>
            <button className="btn btn-ghost btn-small" onClick={onClose}>Close</button>
          </div>
          <p className="hint">{error}</p>
        </section>
      </div>
    );
  }

  return (
    <div className="reader-overlay" onClick={onClose}>
      <section className="card reader" onClick={(e) => e.stopPropagation()}>
        <div className="reader-bar">
          <button className="btn btn-primary btn-small" onClick={toggle}>
            {playing ? "Pause" : "Play"}
          </button>
          <select
            className="speed-pick"
            value={rate}
            onChange={(e) => setRate(Number(e.target.value))}
            aria-label="Playback speed"
            title="Playback speed"
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}&times;
              </option>
            ))}
          </select>
          <label className="reader-follow">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            Follow
          </label>
          <strong className="reader-title">{job.title ?? job.epub_filename}</strong>
          <button className="btn btn-ghost btn-small" onClick={onClose}>Close</button>
        </div>

        {chapters && chapters.length > 1 && (
          <div className="reader-nav">
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => stepChapter(-1)}
              title="Previous chapter"
            >
              ⏮
            </button>
            {/* A select rather than a list: the list would be another long
                scroll inside a page that is already one, and this one folds
                away on a phone by itself. */}
            <select
              className="reader-chapter-pick"
              value={chapterIndex < 0 ? 0 : chapterIndex}
              onChange={(e) => goToChapter(Number(e.target.value))}
              aria-label="Chapter"
            >
              {chapters.map((ch, i) => (
                <option key={i} value={i}>
                  {ch.title}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-ghost btn-small"
              onClick={() => stepChapter(1)}
              title="Next chapter"
            >
              ⏭
            </button>
          </div>
        )}

        <div className="reader-body" ref={body}>
          {/* One highlight for the whole book, moved rather than redrawn. */}
          <div className="reader-cursor" ref={cursor} aria-hidden="true" />
          {!chapters && <p className="hint">Opening the book…</p>}
          {chapters && !chapter && (
            <p className="hint">
              {loading ? "Fetching this chapter…" : "Nothing to show yet."}
            </p>
          )}
          {chapter && (
            <section className="reader-chapter">
              <h3 className="reader-chapter-title" onClick={() => jumpTo(chapter.start)}>
                {chapter.title}
              </h3>
              {!chapter.aligned && (
                <p className="hint">
                  This chapter's text and recording could not be matched up, so it
                  won't follow along.
                </p>
              )}
              {chapter.blocks.map((b, bi) => (
                <Block
                  key={bi}
                  block={b}
                  blockKey={`b${bi}`}
                  activeChunk={
                    current?.blockKey === `b${bi}` ? current.chunkIndex : -1
                  }
                  onJump={jumpTo}
                />
              ))}
            </section>
          )}
        </div>
      </section>
    </div>
  );
}

function Block({
  block,
  blockKey,
  activeChunk,
  onJump,
}: {
  block: ReadChapter["blocks"][number];
  blockKey: string;
  activeChunk: number;
  onJump: (seconds: number) => void;
}) {
  const Tag = (["h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"].includes(block.tag)
    ? block.tag
    : "p") as keyof JSX.IntrinsicElements;

  /* The book's own markup, with the spoken words wrapped inside it.
   *
   * One path for every paragraph now. Formatted ones used to be excluded from
   * word-level following altogether, because a sentence split would cut
   * through an <em> or a link — and that turned out to describe most of a real
   * book rather than an awkward minority, so the highlight almost never
   * appeared. Wrapping words instead of splitting sentences leaves the markup
   * exactly as it was.
   *
   * Clicking anywhere in the paragraph jumps to it; the individual words are
   * targets too, handled by the wrapper below. */
  if (block.words.length) {
    // Collected into one object because `Tag` is any of a dozen element types,
    // and a handler written inline has to satisfy every one of their signatures
    // at once.
    const props = {
      "data-key": blockKey,
      className: "block",
      onClick: (e: React.MouseEvent) => {
        const word = (e.target as HTMLElement).closest<HTMLElement>("[data-w]");
        const index = word ? Number(word.dataset.w) : -1;
        if (index >= 0 && block.words[index]) onJump(block.words[index][0]);
      },
      dangerouslySetInnerHTML: { __html: block.html },
    } as Record<string, unknown>;
    return <Tag {...props} />;
  }

  /* No words: narrated before they were timed, or the page and the recording
     disagreed. Fall back to lighting the sentence — or the whole paragraph
     where its markup makes sentences unsplittable. */
  if (block.inline || block.chunks.length <= 1) {
    return (
      <Tag
        data-key={blockKey}
        className={activeChunk >= 0 ? "block speaking" : "block"}
        onClick={() => block.chunks[0] && onJump(block.chunks[0].start)}
        dangerouslySetInnerHTML={{ __html: block.html }}
      />
    );
  }

  return (
    <Tag data-key={blockKey} className="block">
      {block.chunks.map((c, i) => (
        <span
          key={i}
          data-chunk={i}
          className={`chunk${i === activeChunk ? " speaking" : ""}`}
          onClick={() => onJump(c.start)}
          title="Jump here"
        >
          {c.text}{" "}
        </span>
      ))}
    </Tag>
  );
}
