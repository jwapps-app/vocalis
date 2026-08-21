import { useEffect, useMemo, useRef, useState } from "react";
import { downloadUrl, getReadable, Job, ReadChapter } from "./api";
import { seekWhenReady } from "./media";

/**
 * Read the book while it is narrated, with the sentence being spoken lit up.
 *
 * The text is the book's own markup — italics, headings, verse — not a
 * flattened transcript. Each block carries the moments its sentences are read,
 * captured during synthesis, so following the audio is a lookup rather than a
 * guess.
 */
type Word = { text: string; start: number; end: number };
type Span = {
  start: number;
  end: number;
  blockKey: string;
  chunkIndex: number;
  words: Word[];
};

export default function Reader({ job, onClose }: { job: Job; onClose: () => void }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const body = useRef<HTMLDivElement | null>(null);
  const [chapters, setChapters] = useState<ReadChapter[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
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
    chapters?.forEach((ch, ci) =>
      ch.blocks.forEach((b, bi) =>
        b.chunks.forEach((c, idx) =>
          out.push({
            start: c.start, end: c.end, blockKey: `${ci}-${bi}`,
            chunkIndex: idx, words: c.words ?? [],
          })
        )
      )
    );
    return out;
  }, [chapters]);

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

  /* The word being spoken, within the sentence already found.
   *
   * A separate, tiny search rather than one flat list of every word in the
   * book: a nine-hour book runs to ninety thousand words, and this runs on
   * every timeupdate. Searching the twenty-odd words of the current sentence
   * is work that does not grow with the length of the book.
   *
   * -1 when the book has no word timings, or between words — the gaps are
   * real, since the narrator pauses — and the highlight simply waits where it
   * is rather than jumping ahead. */
  const currentWord = useMemo(() => {
    if (!current || !current.words.length) return -1;
    let found = -1;
    for (let i = 0; i < current.words.length; i++) {
      if (at >= current.words[i].start) found = i;
      else break;
    }
    return found;
  }, [current, at]);

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

  /* Move the page as well as the audio.
   *
   * The whole book is one scroll, so jumping the recording without jumping the
   * text would leave the reader looking at wherever they had scrolled to. The
   * scroll is unconditional rather than left to the follow effect, which only
   * runs when Follow is ticked — turning it off should stop the page drifting
   * on its own, not stop it obeying a chapter you chose. */
  function goToChapter(index: number) {
    const chapter = chapters?.[index];
    const el = audio.current;
    if (!chapter || !el) return;
    // Unlike clicking a sentence, this does not start playing. Moving about
    // the book is as often reading as it is listening, and a paused reader who
    // picks a chapter has not asked for sound.
    seekWhenReady(el, chapter.start);
    setAt(chapter.start);
    body.current
      ?.querySelector<HTMLElement>(`[data-chapter="${index}"]`)
      ?.scrollIntoView({ block: "start", behavior: "smooth" });
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
          {!chapters && <p className="hint">Opening the book…</p>}
          {chapters?.map((ch, ci) => (
            <section key={ci} className="reader-chapter" data-chapter={ci}>
              <h3 className="reader-chapter-title" onClick={() => jumpTo(ch.start)}>
                {ch.title}
              </h3>
              {!ch.aligned && (
                <p className="hint">
                  This chapter's text and recording could not be matched up, so it
                  won't follow along.
                </p>
              )}
              {ch.blocks.map((b, bi) => (
                <Block
                  key={bi}
                  block={b}
                  blockKey={`${ci}-${bi}`}
                  activeChunk={
                    current?.blockKey === `${ci}-${bi}` ? current.chunkIndex : -1
                  }
                  activeWord={
                    current?.blockKey === `${ci}-${bi}` ? currentWord : -1
                  }
                  onJump={jumpTo}
                />
              ))}
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}

function Block({
  block,
  blockKey,
  activeChunk,
  activeWord,
  onJump,
}: {
  block: ReadChapter["blocks"][number];
  blockKey: string;
  activeChunk: number;
  activeWord: number;
  onJump: (seconds: number) => void;
}) {
  const Tag = (["h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "li"].includes(block.tag)
    ? block.tag
    : "p") as keyof JSX.IntrinsicElements;

  /* The book's own markup, kept intact.
   *
   * Used whenever the paragraph carries inline formatting — an <em> or a link
   * could straddle a sentence boundary, and splitting that would either break
   * the HTML or throw the emphasis away. The book should look like the book,
   * so the whole paragraph lights up instead: a coarser highlight, never a
   * mangled page.
   *
   * A single-sentence paragraph used to come here too, on the grounds that
   * there was nothing finer to distinguish. Words are finer, so it does not
   * any more — only markup sends a paragraph down this path now. */
  if (block.inline) {
    const speaking = activeChunk >= 0;
    return (
      <Tag
        data-key={blockKey}
        className={speaking ? "block speaking" : "block"}
        onClick={() => block.chunks[0] && onJump(block.chunks[0].start)}
        dangerouslySetInnerHTML={{ __html: block.html }}
      />
    );
  }

  // Plain prose: split into sentences, and each sentence into its words where
  // the narrator timed them, so the highlight sits on the word being spoken
  // rather than the whole sentence. Nothing is lost — there was no inline
  // markup to keep, and the text shown is the text that was read.
  return (
    <Tag data-key={blockKey} className="block">
      {block.chunks.map((c, i) => (
        <span
          key={i}
          data-chunk={i}
          className="chunk"
          title="Jump here"
        >
          {c.words.length ? (
            c.words.map((w, wi) => (
              <span
                key={wi}
                className={
                  i === activeChunk && wi === activeWord ? "word speaking" : "word"
                }
                onClick={() => onJump(w.start)}
              >
                {w.text}{" "}
              </span>
            ))
          ) : (
            /* Narrated before words were timed: the sentence is the finest
               thing this book knows about, so light all of it. */
            <span
              className={i === activeChunk ? "speaking" : undefined}
              onClick={() => onJump(c.start)}
            >
              {c.text}{" "}
            </span>
          )}
        </span>
      ))}
    </Tag>
  );
}
