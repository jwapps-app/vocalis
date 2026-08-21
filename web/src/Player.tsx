import { useEffect, useMemo, useRef, useState } from "react";
import { ChapterMark, downloadUrl, getChapterMarks, Job } from "./api";
import { rememberRate, seekWhenReady, SPEEDS, storedRate } from "./media";

/**
 * Listen to a finished book without leaving Vocalis.
 *
 * The audio streams from the same endpoint the download button uses — it
 * serves Range requests, so seeking works without pulling the whole file.
 *
 * Chapters come from the marks recorded during narration, so skipping lands on
 * a real chapter boundary rather than a guess. A book narrated before those
 * were recorded simply gets the transport controls and no list — an unmarked
 * book is not given invented chapter points.
 */
function fmt(seconds: number): string {
  if (!isFinite(seconds)) return "0:00";
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return h > 0 ? `${h}:${mm}:${String(s).padStart(2, "0")}` : `${mm}:${String(s).padStart(2, "0")}`;
}

export default function Player({ job, onClose }: { job: Job; onClose: () => void }) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const list = useRef<HTMLOListElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
  const [rate, setRate] = useState(storedRate);
  // The M4B carries its own duration; fall back to the stored figure until the
  // metadata loads, so the scrubber is not stuck at 0:00 on first paint.
  const [total, setTotal] = useState(job.audio_seconds ?? 0);
  const [chapters, setChapters] = useState<ChapterMark[]>([]);

  useEffect(() => {
    if (!job.has_timings) return;
    // A missing chapter list costs the listener nothing but the list itself,
    // so a failure here stays silent rather than covering the player.
    getChapterMarks(job.id).then((r) => setChapters(r.chapters), () => {});
  }, [job.id, job.has_timings]);

  useEffect(() => {
    const el = new Audio(downloadUrl(job));
    el.preload = "metadata";
    el.playbackRate = rate;
    audio.current = el;
    const onMeta = () => { if (isFinite(el.duration)) setTotal(el.duration); };
    const onTime = () => setAt(el.currentTime);
    const onEnd = () => setPlaying(false);
    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("ended", onEnd);
    return () => {
      el.pause();
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("ended", onEnd);
      audio.current = null;
    };
  }, [job.id]);


  // Applied to the element that already exists, not only at creation, so
  // changing it mid-sentence takes effect on the words being spoken.
  useEffect(() => {
    if (audio.current) audio.current.playbackRate = rate;
    rememberRate(rate);
  }, [rate]);

  function toggle() {
    const el = audio.current;
    if (!el) return;
    if (el.paused) { el.play(); setPlaying(true); }
    else { el.pause(); setPlaying(false); }
  }

  function skip(delta: number) {
    const el = audio.current;
    if (el) seekWhenReady(el, Math.max(0, Math.min(total, el.currentTime + delta)));
  }

  function seek(e: React.ChangeEvent<HTMLInputElement>) {
    const el = audio.current;
    if (el) { seekWhenReady(el, Number(e.target.value)); setAt(Number(e.target.value)); }
  }

  function jumpTo(seconds: number) {
    const el = audio.current;
    if (!el) return;
    seekWhenReady(el, seconds);
    setAt(seconds);
  }

  /* The last chapter that has already begun. Chapters are in order and a book
     runs to tens of them, not thousands, so a scan from the end is plenty. */
  const currentIndex = useMemo(() => {
    for (let i = chapters.length - 1; i >= 0; i--) {
      if (at >= chapters[i].start) return i;
    }
    return chapters.length ? 0 : -1;
  }, [chapters, at]);

  /* Keep the playing chapter in view — but only when it actually changes.
     Position updates arrive four times a second, and scrolling on each of them
     would drag the list out from under anyone browsing it. */
  useEffect(() => {
    list.current
      ?.querySelector<HTMLElement>("[data-current]")
      ?.scrollIntoView({ block: "nearest" });
  }, [currentIndex]);

  /* Back near the top of a chapter means "the previous one" — pressing it once
     restarts the chapter you are in, pressing it again leaves. Same rule every
     other player uses, and it makes an accidental press recoverable. */
  function step(delta: number) {
    if (currentIndex < 0) return;
    const atStart = at - chapters[currentIndex].start < 3;
    const target =
      delta < 0 && !atStart ? currentIndex : Math.min(
        chapters.length - 1, Math.max(0, currentIndex + delta)
      );
    jumpTo(chapters[target].start);
  }

  return (
    <div className="player-overlay" onClick={onClose}>
      <section className="card player" onClick={(e) => e.stopPropagation()}>
        <div className="player-head">
          <div>
            <h2 className="book-title">{job.title ?? job.epub_filename}</h2>
            {job.author && <p className="hint">{job.author}</p>}
          </div>
          <button type="button" className="btn btn-ghost btn-small" onClick={onClose}>
            Close
          </button>
        </div>

        <input
          className="player-scrub"
          type="range"
          min={0}
          max={total || 0}
          step={1}
          value={at}
          onChange={seek}
          aria-label="Position"
        />
        <div className="player-times">
          <span>{fmt(at)}</span>
          <span>{fmt(total)}</span>
        </div>

        <div className="player-controls">
          {chapters.length > 0 && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => step(-1)}
              title="Previous chapter"
            >
              ⏮
            </button>
          )}
          <button type="button" className="btn btn-ghost" onClick={() => skip(-30)}>
            −30s
          </button>
          <button type="button" className="btn btn-primary player-play" onClick={toggle}>
            {playing ? "Pause" : "Play"}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => skip(30)}>
            +30s
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

          {chapters.length > 0 && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => step(1)}
              title="Next chapter"
            >
              ⏭
            </button>
          )}
        </div>

        {chapters.length > 0 && (
          <>
            <p className="player-now">
              {chapters[currentIndex]?.title ?? ""}
            </p>
            <ol className="player-chapters" ref={list}>
              {chapters.map((ch, i) => (
                <li key={ch.index}>
                  <button
                    type="button"
                    className={`player-chapter${i === currentIndex ? " current" : ""}`}
                    onClick={() => jumpTo(ch.start)}
                    data-current={i === currentIndex ? "" : undefined}
                  >
                    <span className="player-chapter-title">{ch.title}</span>
                    <span className="player-chapter-time">{fmt(ch.start)}</span>
                  </button>
                </li>
              ))}
            </ol>
          </>
        )}

        <a className="btn btn-download" href={downloadUrl(job)}>
          Download M4B
        </a>
      </section>
    </div>
  );
}
