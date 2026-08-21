/**
 * Seek, even if the recording has not finished loading.
 *
 * Setting currentTime before the browser has the file's metadata is silently
 * clamped to the start, and the timeupdate that follows drags the UI back to
 * 0:00 with it — so pressing a chapter button in the first moments after
 * opening a book looked like it did nothing whatsoever. Nothing errors; the
 * jump is simply dropped.
 *
 * That window is not small in practice. These are audiobooks: a long one is a
 * few hundred megabytes served from a NAS over a home network, and the
 * metadata is not there the instant the panel appears.
 *
 * Deferring the seek to `loadedmetadata` makes it land instead. Repeated
 * presses register their own listener each, and since they run in the order
 * they were added the last press is the one that sticks — which is the one the
 * reader meant.
 */
export function seekWhenReady(el: HTMLAudioElement, seconds: number): void {
  if (el.readyState >= 1 /* HAVE_METADATA */) {
    el.currentTime = seconds;
    return;
  }
  el.addEventListener(
    "loadedmetadata",
    () => {
      el.currentTime = seconds;
    },
    { once: true }
  );
}


/** Playback speeds offered. Wider than a video player's because a narrated
 *  book is listened to for hours, and a reader following the text has a
 *  different comfortable pace from someone only listening. */
export const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2] as const;

const RATE_KEY = "vocalis:rate";

/** The last speed chosen, or normal. Remembered because picking it again at
 *  the start of every book would be tedious over a long one. */
export function storedRate(): number {
  const saved = Number(localStorage.getItem(RATE_KEY));
  return SPEEDS.includes(saved as (typeof SPEEDS)[number]) ? saved : 1;
}

export function rememberRate(rate: number): void {
  try {
    localStorage.setItem(RATE_KEY, String(rate));
  } catch {
    /* private browsing, or storage full — the speed just will not persist */
  }
}
