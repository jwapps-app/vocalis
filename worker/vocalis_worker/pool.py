"""Parallel chapter synthesis.

Chatterbox decodes one audio token at a time (~19 steps/sec on an M4 Pro), so
a single stream leaves most of the GPU idle waiting on sequential
dependencies. Rendering several chapters at once fills that gap.

Processes, not threads: PyTorch's Python-level decode loop holds the GIL often
enough that threads barely overlap, and one model instance is not safe to
share. Each pool process loads its own model, so concurrency costs memory —
which is why it is a user-facing setting and why it is capped here.

The audio does not depend on this setting. Every chunk re-seeds with the job's
fixed seed and the same reference clip, so a chapter renders identically no
matter which process handles it or in what order.

multiprocessing.Pool rather than ProcessPoolExecutor: only Pool exposes
terminate(), which lets a cancel stop mid-chapter instead of waiting out the
chapter in flight.

Memory
------
Two separate problems, diagnosed in the wrong order at some cost.

1. The allocator hoards. PyTorch's MPS caching allocator defaults to a high
   watermark of 1.7x the device's recommended working set — 30 GB on a 24 GB
   Mac — and Chatterbox takes it: 25.0 GB reserved against 3.0 GB of live
   tensors. Unified memory means that is system RAM, so the machine started
   force-quitting applications. Capping the watermark fixes this at no cost
   (per-chunk time 17.2s vs 17.5s over 8 chunks).

2. Something outside the allocator grows, and this is what actually killed
   books mid-run. Metal's MPSGraph compilation cache holds a compiled graph per
   distinct input shape; chapters are full of differently-sized chunks, so it
   grows continuously. It is invisible to empty_cache() and to
   driver_allocated_memory(), surfacing only as the OOM message's "other
   allocations" — which climbed 1.5 -> 11.8 GB across one book while this
   process's own `MPS allocated` stayed flat at ~4.5 GB. Only restarting the
   process clears it, hence SEGMENTS_PER_PROCESS.

The failures looked like they were about chapter length and were not: four of
five OOMs hit ~3k-character chapters while the same book's 10k-character
chapters rendered fine. What they tracked was cumulative chunks in one
process. Two corollaries worth keeping in mind:

- The watermark is a DEVICE-WIDE ceiling counting every app's GPU use, not a
  private per-process budget. Tightening it makes starvation arrive sooner,
  and subtracting a "system reserve" from it double-counts other apps.
- Any memory test here must use VARIED chunk lengths. A test that repeats one
  string compiles a single graph and shows perfectly flat memory while
  reproducing nothing.
"""

import logging
import multiprocessing
import os
from pathlib import Path

log = logging.getLogger(__name__)

# The watermark is a DEVICE-WIDE ceiling, not a private per-process budget.
# PyTorch enforces it against Metal's total allocation, which includes every
# other app on the Mac — the OOM message spells this out as "other
# allocations", and on a working desktop that was 9.5 GB of browser, editor and
# chat windows. Dividing a "budget" by concurrency and subtracting a system
# reserve therefore double-counted those apps: they ate into the cap *and* were
# reserved around, so the tighter the cap the sooner their normal growth
# starved the render process. Books failed later and later into the run as the
# desktop filled up, which is exactly the signature that misled several
# earlier fixes.
#
# What actually bounds this process is inference_mode plus empty_cache between
# chunks: measured flat at ~3.0 GB live / ~6.7 GB reserved across 8 chapters,
# with no upward drift. The ceiling exists only to stop pathological hoarding
# (the original 25 GB reservation that froze the machine), so it is set near
# the device maximum and left alone.
DEVICE_CEILING_FRACTION = 0.92

# Headroom a rendering process needs, measured: ~3 GB live, ~6.7 GB reserved.
# Used to decide how many will fit in RAM — not to set the ceiling.
PROCESS_FOOTPRINT_GB = 7.0

# Left free for macOS and everything else when deciding concurrency. Applies to
# system RAM, which is a real per-process cost, unlike the GPU ceiling above.
SYSTEM_RESERVE_GB = 8.0

# GPU allocation one render process peaks at, from real OOM reports (4.0–5.4 GB
# of live pool memory on the longest chapters).
PROCESS_GPU_GB = 5.5

# Fallback for when free GPU cannot be measured. Observed climbing from 1.5 GB
# on a quiet machine to 9.5 GB with a browser, editor and chat app open; the
# pessimistic end is the safe default, because every process shares one device
# ceiling and a book that dies hours in costs more than one run at half speed.
# When a measurement is available it is always preferred — this constant threw
# away most of a quiet machine's capacity.
OTHER_APPS_GPU_GB = 8.0

# Held back from the measurement so the desktop can grow mid-book without
# starving a render already under way. A snapshot at job start is not a promise
# about the next three hours.
GPU_SAFETY_MARGIN_GB = 2.5

# Recycle a render process after this many segments. This is load-bearing, not
# a precaution: Metal's MPSGraph compilation cache holds a compiled graph per
# distinct input shape, and chapters are full of differently-sized chunks. That
# cache lives outside PyTorch's allocator, so empty_cache() never frees it and
# driver_allocated_memory() never shows it — it surfaces only as the OOM
# message's "other allocations", which climbed 1.5 -> 11.8 GB over one book
# while the process's own usage sat flat at ~4.5 GB. Restarting the process is
# the only thing that clears it.
#
# Counted in segments rather than chapters on purpose: a chapter is not a
# bounded amount of work. A 40k-character chapter is ~133 chunks and would
# exhaust the cache by itself, long before a per-chapter recycle could fire.
# Segments are a fixed chunk count (config.SEGMENT_CHUNKS), so 2 per process
# caps a process at ~40 chunks against a measured ~73-chunk ceiling, whatever
# the book's chapter length.
SEGMENTS_PER_PROCESS = 2

# Apple Silicon reports recommendedMaxWorkingSetSize at ~0.74x physical RAM,
# and the watermark env vars are ratios of that. They must be set before the
# MPS allocator initializes, i.e. before torch is imported, so the ratio cannot
# simply ask torch what the recommendation is.
_RECOMMENDED_MAX_FRACTION = 0.74


def physical_memory_gb() -> float:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3


def device_ceiling_gb() -> float:
    """Total GPU allocation allowed on the device, all apps included."""
    return _RECOMMENDED_MAX_FRACTION * physical_memory_gb() * DEVICE_CEILING_FRACTION


def safe_concurrency(requested: int, free_gpu_gb: float | None = None) -> int:
    """Clamp requested concurrency to what this machine can actually hold.

    Two independent limits. RAM, because each process carries its own copy of
    the model. And GPU headroom, because every process shares one device
    ceiling with the rest of the desktop — that one usually binds first, and it
    is the reason a machine's capacity has to be measured rather than assumed.
    Pass `free_gpu_gb` from a live probe; without it this falls back to
    assuming a busy desktop, which is safe but wastes an idle machine.
    """
    requested = max(1, requested)
    by_ram = int((physical_memory_gb() - SYSTEM_RESERVE_GB) // PROCESS_FOOTPRINT_GB)
    if free_gpu_gb is None:
        headroom = device_ceiling_gb() - OTHER_APPS_GPU_GB
        basis = "assumed"
    else:
        headroom = free_gpu_gb - GPU_SAFETY_MARGIN_GB
        basis = "measured"
    by_gpu = int(headroom // PROCESS_GPU_GB)
    allowed = max(1, min(by_ram, by_gpu))
    if requested > allowed:
        log.warning(
            "Concurrency %d exceeds capacity (RAM allows %d; %s GPU headroom"
            " %.1f GB allows %d) — using %d",
            requested, max(1, by_ram), basis, max(0.0, headroom),
            max(1, by_gpu), allowed,
        )
    return min(requested, allowed)


def _mps_env(concurrency: int) -> dict[str, str]:
    """The device-wide GPU ceiling. Not per-process, and not divided by
    concurrency — see the note at the top of this module."""
    ratio = DEVICE_CEILING_FRACTION
    return {
        "PYTORCH_MPS_HIGH_WATERMARK_RATIO": f"{ratio:.3f}",
        # Start reclaiming cached blocks below the ceiling rather than at it.
        "PYTORCH_MPS_LOW_WATERMARK_RATIO": f"{ratio * 0.7:.3f}",
    }


_synth = None  # one Synthesizer per pool process


def _init_worker() -> None:
    global _synth
    # Sampling progress bars from N processes at once are unreadable noise.
    os.environ.setdefault("TQDM_DISABLE", "1")
    # PYTORCH_MPS_*_WATERMARK_RATIO come from the parent's environment, which
    # ChapterPool sets before spawning — they have to be in place before torch
    # is imported here, which is why that import is inside this function.
    from .synth import Synthesizer

    _synth = Synthesizer()


def _render(args) -> tuple[int, float]:
    """Synthesize one chapter inside a pool process. Returns (index, seconds)."""
    index, chunks, voice_ref, seed, out_path, params, trailing_pause = args
    import torch

    def release() -> None:
        # Hand the chapter's working set back; the process lives for the whole
        # job, so anything held here is held for hours.
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()

    try:
        duration = _synth.synth_chapter(
            chunks, Path(voice_ref) if voice_ref else None, seed, Path(out_path),
            params=params, trailing_pause=trailing_pause,
        )
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        # An unlucky long chapter can outgrow the ceiling while the rest of the
        # desktop holds GPU memory. Drop the cache and try once more before
        # failing a job that may already be hours in.
        #
        # Log the allocator's own numbers: a retry that swallows them leaves no
        # way to tell "this process is using too much" from "everything else on
        # the Mac is", which are opposite fixes.
        log.warning("Chapter %d ran out of GPU memory; retrying once — %s", index, exc)
        release()
        duration = _synth.synth_chapter(
            chunks, Path(voice_ref) if voice_ref else None, seed, Path(out_path),
            params=params, trailing_pause=trailing_pause,
        )
    release()
    return index, duration


def cached_seconds(path: Path) -> float | None:
    """Duration of an already-rendered chapter, or None if unusable.

    A file truncated by a kill or crash reads as unusable and gets re-rendered
    rather than corrupting the finished book.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        import torchaudio

        info = torchaudio.info(str(path))
        return info.num_frames / info.sample_rate if info.num_frames else None
    except Exception:
        log.warning("Discarding unreadable cached chapter %s", path.name)
        return None


class Cancelled(Exception):
    """Raised when a job's cancel flag is seen mid-render."""


class ChapterPool:
    """Renders chapters across N processes, each holding its own model."""

    def __init__(self, concurrency: int, free_gpu_gb: float | None = None):
        self.concurrency = safe_concurrency(concurrency, free_gpu_gb)
        self._pool = None

    def __enter__(self):
        # Exported before spawning so the children inherit them no matter when
        # torch first gets imported in the child.
        os.environ.update(_mps_env(self.concurrency))
        log.info(
            "Loading model into %d process(es); GPU ceiling %.1f GB (shared with"
            " everything else on the Mac)",
            self.concurrency, device_ceiling_gb(),
        )
        ctx = multiprocessing.get_context("spawn")
        # maxtasksperchild recycles a worker after this many chapters, reloading
        # the model fresh. Belt to inference_mode's braces: if any GPU memory
        # still creeps up across chapters, a bounded process lifetime caps it
        # instead of letting it run to an OOM 20 chapters into a book. The cost
        # is one model reload (~75s) per recycle, cheap against re-narrating a
        # failed book. Kept high enough that most books never trigger it.
        self._pool = ctx.Pool(
            processes=self.concurrency,
            initializer=_init_worker,
            maxtasksperchild=SEGMENTS_PER_PROCESS,
        )
        return self

    def __exit__(self, *exc):
        if self._pool is not None:
            self._pool.terminate()
            self._pool.join()
        return False

    def render(self, tasks, on_done, should_cancel, poll_seconds: float = 2.0) -> None:
        """Render every task, calling on_done(index, duration) as each lands.

        Polls should_cancel() while waiting so a cancel takes effect within a
        couple of seconds — terminating the pool kills chapters in flight,
        whose partial files are simply never written.
        """
        import time

        pending = [
            (args[0], self._pool.apply_async(_render, (args,)))
            for args in tasks
        ]
        while pending:
            if should_cancel():
                raise Cancelled()
            still_pending = []
            for index, result in pending:
                if not result.ready():
                    still_pending.append((index, result))
                    continue
                on_done(*result.get())  # re-raises worker exceptions here
            pending = still_pending
            if pending:
                time.sleep(poll_seconds)
