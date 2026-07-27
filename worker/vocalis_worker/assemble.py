"""Concatenate chapter WAVs into a chaptered M4B via ffmpeg."""

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _ffmetadata(title: str, author: str, chapters: list[tuple[str, float]]) -> str:
    def esc(value: str) -> str:
        # Backslash first, or the escapes below get double-escaped. \r matters
        # as much as \n: ffmetadata is line-oriented and a title carrying a bare
        # carriage return — an EPUB can supply one — could start a line the
        # parser reads as a directive.
        for ch in ("\\", "=", ";", "#", "\n", "\r"):
            value = value.replace(ch, "\\" + ch)
        return value

    lines = [
        ";FFMETADATA1",
        f"title={esc(title)}",
        f"artist={esc(author)}",
        f"album={esc(title)}",
        "genre=Audiobook",
    ]
    start_ms = 0
    for chapter_title, duration in chapters:
        end_ms = start_ms + int(duration * 1000)
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={esc(chapter_title)}",
        ]
        start_ms = end_ms
    return "\n".join(lines) + "\n"


def normalize_inputs(wavs: list[Path]) -> None:
    """Force every cached WAV to the same sample format, in place.

    ffmpeg's concat *demuxer* is a stream-level splice: it configures one
    decoder from the first input and pushes the rest through it, so inputs that
    differ in sample format silently decode as garbage. A cache holding both
    32-bit float chapters (written before the 16-bit change) and 16-bit ones
    produced exactly that — the AAC encoder rejected the result as "(near)
    NaN/+-Inf" at the precise second the format changed, an hour and a half in.

    Rewriting is cheap and lossless in the ways that matter here: the audio is
    already destined for 64 kbps AAC, and 16-bit is what the current renderer
    writes anyway. Re-narrating to fix a container detail would cost hours.
    """
    import torch  # local: this module is imported by the API too
    import torchaudio

    for path in wavs:
        try:
            if torchaudio.info(str(path)).bits_per_sample == 16:
                continue
            audio, sample_rate = torchaudio.load(str(path))
            # Guard the encoder against anything non-finite, whatever its origin.
            audio = torch.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0).clamp(-1.0, 1.0)
            torchaudio.save(str(path), audio, sample_rate,
                            encoding="PCM_S", bits_per_sample=16)
            log.info("Normalized %s to 16-bit for assembly", path.name)
        except Exception:
            log.exception("Could not normalize %s; assembly may fail", path.name)


def assemble_m4b(
    chapter_wavs: list[Path],
    chapters: list[tuple[str, float]],  # (title, duration_seconds), same order
    title: str,
    author: str,
    cover: Path | None,
    work_dir: Path,
    out_path: Path,
    bitrate: str = "64k",
) -> None:
    normalize_inputs(chapter_wavs)

    concat_file = work_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in chapter_wavs)
    )
    meta_file = work_dir / "ffmetadata.txt"
    meta_file.write_text(_ffmetadata(title, author, chapters))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-i", str(meta_file),
    ]
    if cover is not None:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a", "-map_metadata", "1", "-c:a", "aac", "-b:a", bitrate]
    if cover is not None:
        cmd += ["-map", "2:v", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    cmd += ["-f", "ipod", str(out_path)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")
