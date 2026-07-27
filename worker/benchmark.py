#!/usr/bin/env python3
"""Benchmark one representative chapter and project full-book synthesis time.

Run this on the worker host before committing to a full-book job:

    python benchmark.py path/to/book.epub [--voice ref.wav] [--chunks 10]

Reports wall time, realtime factor, and a linear projection for the whole book.
"""

import argparse
import time
from pathlib import Path

from vocalis_worker import config
from vocalis_core.epub_parse import parse_epub
from vocalis_worker.synth import Synthesizer
from vocalis_core.text_clean import clean_text, chunk_text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("epub", type=Path)
    ap.add_argument("--voice", type=Path, default=None, help="optional reference voice clip")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--chunks", type=int, default=0,
                    help="limit to first N chunks of the chapter (0 = whole chapter)")
    ap.add_argument("--out", type=Path, default=Path("benchmark_chapter.wav"))
    args = ap.parse_args()

    book = parse_epub(args.epub)
    chapter_chunks = [chunk_text(clean_text(c.text), config.MAX_CHUNK_CHARS) for c in book.chapters]
    total_chars = sum(sum(len(ch) for ch in chunks) for chunks in chapter_chunks)

    # Representative chapter = median by character count.
    sizes = sorted(range(len(chapter_chunks)), key=lambda i: sum(len(c) for c in chapter_chunks[i]))
    idx = sizes[len(sizes) // 2]
    chunks = chapter_chunks[idx]
    if args.chunks:
        chunks = chunks[: args.chunks]
    chunk_chars = sum(len(c) for c in chunks)

    print(f"Book: {book.title} — {len(book.chapters)} chapters, {total_chars:,} chars")
    print(f"Benchmarking chapter {idx + 1} ({book.chapters[idx].title!r}): "
          f"{len(chunks)} chunks, {chunk_chars:,} chars")

    synth = Synthesizer()
    started = time.monotonic()
    audio_seconds = synth.synth_chapter(chunks, args.voice, args.seed, args.out)
    wall = time.monotonic() - started

    projected = wall / chunk_chars * total_chars
    print(f"\nDevice: {synth.device}")
    print(f"Wall time: {wall:,.1f}s for {audio_seconds:,.1f}s of audio "
          f"(realtime factor {audio_seconds / wall:.2f}x)")
    print(f"Throughput: {chunk_chars / wall:.1f} chars/s")
    print(f"Projected full book: {projected / 3600:.1f}h "
          f"({'within' if projected < 24 * 3600 else 'EXCEEDS'} the 24h target)")
    print(f"Sample audio written to {args.out}")


if __name__ == "__main__":
    main()
