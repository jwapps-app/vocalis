import { useEffect, useRef, useState } from "react";
import { downloadUrl, Job } from "./api";

/**
 * Listen to a finished book without leaving Vocalis.
 *
 * The audio streams from the same endpoint the download button uses — it
 * serves Range requests, so seeking works without pulling the whole file. This
 * is deliberately a plain transport player: play, scrub, skip. A chapter list
 * and read-along both need timing data the pipeline does not record yet, so
 * they are not faked here.
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
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
  // The M4B carries its own duration; fall back to the stored figure until the
  // metadata loads, so the scrubber is not stuck at 0:00 on first paint.
  const [total, setTotal] = useState(job.audio_seconds ?? 0);

  useEffect(() => {
    const el = new Audio(downloadUrl(job));
    el.preload = "metadata";
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

  function toggle() {
    const el = audio.current;
    if (!el) return;
    if (el.paused) { el.play(); setPlaying(true); }
    else { el.pause(); setPlaying(false); }
  }

  function skip(delta: number) {
    const el = audio.current;
    if (el) el.currentTime = Math.max(0, Math.min(total, el.currentTime + delta));
  }

  function seek(e: React.ChangeEvent<HTMLInputElement>) {
    const el = audio.current;
    if (el) { el.currentTime = Number(e.target.value); setAt(Number(e.target.value)); }
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
          <button type="button" className="btn btn-ghost" onClick={() => skip(-30)}>
            −30s
          </button>
          <button type="button" className="btn btn-primary player-play" onClick={toggle}>
            {playing ? "Pause" : "Play"}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => skip(30)}>
            +30s
          </button>
        </div>

        <a className="btn btn-download" href={downloadUrl(job)}>
          Download M4B
        </a>
      </section>
    </div>
  );
}
