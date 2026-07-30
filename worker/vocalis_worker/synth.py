"""Chatterbox TTS wrapper — native Metal (MPS) execution."""

import logging
from pathlib import Path

import torch
import torchaudio

log = logging.getLogger(__name__)


def pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    log.warning("No GPU backend available — falling back to CPU (this will be slow)")
    return "cpu"


class Synthesizer:
    def __init__(self, device: str | None = None):
        from chatterbox.tts import ChatterboxTTS

        self.device = device or pick_device()
        log.info("Loading Chatterbox on %s", self.device)
        self.model = ChatterboxTTS.from_pretrained(device=self.device)
        self.sample_rate: int = self.model.sr

    # Narrator preset knobs we forward to Chatterbox's generate().
    ALLOWED_PARAMS = {"exaggeration", "cfg_weight", "temperature"}

    @torch.inference_mode()
    def synth_chunk(
        self, text: str, voice_ref: Path | None, seed: int, params: dict | None = None
    ) -> torch.Tensor:
        # inference_mode, not plain no_grad: without it every generate() call
        # leaves an autograd graph and version-counter bookkeeping alive, and in
        # a process that renders chapter after chapter that accumulates as
        # non-pool GPU memory (MPS "other allocations") until even a 13.5 GB
        # budget OOMs mid-book. Inference needs no gradients at all.
        #
        # Re-seed before every chunk: same seed + same reference clip across the
        # whole book minimizes tone drift between chapters.
        torch.manual_seed(seed)
        kwargs = {k: v for k, v in (params or {}).items() if k in self.ALLOWED_PARAMS}
        if voice_ref is not None:
            kwargs["audio_prompt_path"] = str(voice_ref)
        wav = self.model.generate(text, **kwargs)
        return wav.squeeze(0).cpu()

    def synth_chapter(
        self,
        chunks: list[str],
        voice_ref: Path | None,
        seed: int,
        out_path: Path,
        params: dict | None = None,
        pause_seconds: float = 0.4,
        trailing_pause: bool = False,
    ) -> tuple[float, list[tuple[str, float, float]]]:
        """Synthesize chunks, join with short pauses, write a WAV.

        Returns the duration and where each chunk of text falls within it, as
        (text, start, end) in seconds from the start of this file.

        The timings are a by-product of the concatenation — the offsets are
        already known here — but they cannot be recovered afterwards from the
        audio without speech recognition. Capturing them at synthesis is what
        makes it possible to show which sentence is being read; a book narrated
        without them would have to be narrated again.
        """
        pause = torch.zeros(int(self.sample_rate * pause_seconds))
        parts: list[torch.Tensor] = []
        timings: list[tuple[str, float, float]] = []
        samples = 0
        for i, chunk in enumerate(chunks):
            # Keep each chunk's output on the CPU (synth_chunk already moves it)
            # and release the GPU's cached blocks before the next one. Without
            # this the MPS allocator's freed-but-cached blocks fragment across a
            # long chapter's chunks until an allocation can't fit under the
            # per-process watermark — a late-chapter OOM even though live memory
            # is small. Freeing per chunk holds peak roughly constant regardless
            # of chapter length.
            spoken = self.synth_chunk(chunk, voice_ref, seed, params)
            start = samples / self.sample_rate
            samples += spoken.shape[0]
            timings.append((chunk, start, samples / self.sample_rate))
            parts.append(spoken)
            if i < len(chunks) - 1:
                parts.append(pause)
                # The gap belongs to neither chunk, but it does move the next
                # one later, so it has to be counted.
                samples += pause.shape[0]
            if self.device == "mps":
                torch.mps.empty_cache()
        # A chapter rendered as several segments would otherwise lose the pause
        # at every segment boundary — audible as chunks running together every
        # SEGMENT_CHUNKS. The caller sets this on all but a chapter's last
        # segment, so the joined audio is identical to rendering it in one go.
        if trailing_pause:
            parts.append(pause)
        audio = torch.cat(parts)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 16-bit PCM, not the model's native 32-bit float: the cache is scratch
        # that gets AAC-encoded into the M4B regardless, so the extra 16 bits of
        # a float WAV are pure disk cost — 94 KB/s vs 47 KB/s — with nothing
        # audible to gain for a single narrator voice. Halves the working cache.
        torchaudio.save(
            str(out_path), audio.unsqueeze(0), self.sample_rate,
            encoding="PCM_S", bits_per_sample=16,
        )
        return audio.shape[0] / self.sample_rate, timings
