"""Word-level timings, by forced alignment against the audio we just made.

The narrator already records where each *chunk* — roughly a sentence — starts
and ends, because concatenation hands that over for nothing. Words are a
different problem: nothing in the synthesis loop knows where one word stops and
the next begins.

Interpolating across the chunk was the tempting shortcut and it is not good
enough. Speech is not evenly paced, and Chatterbox inserts real pauses of its
own — one measured chunk had 0.9s and 0.5s of silence sitting mid-sentence.
Spreading time evenly over the characters would put the highlight a second away
from the voice, which is worse than highlighting nothing.

So the audio is aligned against the text that produced it. This is forced
alignment, not recognition: the words are already known, and the only question
is where each one lands, which is the easy half of the problem and accordingly
accurate. Runs on the CPU at around thirty times realtime, so it costs a small
fraction of what synthesis did, and it needs only the audio and the text —
meaning a book narrated before any of this existed can be given word timings
from its cached recordings, without narrating a syllable again.
"""

import logging
import re

log = logging.getLogger(__name__)

# MMS_FA's dictionary is lowercase latin plus the apostrophe. Anything else —
# digits, punctuation, the curly quote a typesetter used — has no token, so it
# is stripped for alignment only. The word the reader sees is the original.
_KEEP = re.compile(r"[^a-z']+")

_pipeline = None


def _load():
    """Fetch the aligner once, on first use.

    Lazy because it is a ~1.2 GB download, and a narrator that never runs a
    book should not pay for it on install.
    """
    global _pipeline
    if _pipeline is None:
        from torchaudio.pipelines import MMS_FA as bundle
        log.info("Loading the word aligner (first use downloads it)")
        _pipeline = (bundle, bundle.get_model(), bundle.get_tokenizer(),
                     bundle.get_aligner())
    return _pipeline


def _normalise(word: str) -> str:
    return _KEEP.sub("", word.lower().replace("’", "'")).strip("'")


def align_words(audio, sample_rate: int, text: str) -> list[dict]:
    """Where each word of `text` is spoken within `audio`.

    Returns [{"text", "start", "end"}] in seconds from the start of the clip,
    carrying the *original* word — punctuation, capitals and all — so the
    reader shows the book rather than the stripped form the aligner needed.

    Returns [] rather than raising if anything goes wrong. A chapter that
    cannot be aligned should read with sentence highlighting, exactly as it did
    before words existed; it should not fail the book.
    """
    import torch
    import torchaudio

    # Every word is kept, whether or not the aligner can take it. The reader
    # renders exactly this list, so dropping the ones with no tokens — "2-6",
    # "&", a bare numeral — would delete them from the page. They are spoken;
    # they simply have no letters for a letter-based aligner to hold on to.
    pieces = [(word, _normalise(word)) for word in text.split()]
    tokens = [clean for _, clean in pieces if clean]
    if not tokens:
        return []

    try:
        bundle, model, tokenizer, aligner = _load()
        if audio.dim() == 1:
            audio = audio.unsqueeze(0)
        resampled = torchaudio.functional.resample(
            audio, sample_rate, bundle.sample_rate
        )
        with torch.inference_mode():
            emission, _ = model(resampled)
            spans = aligner(emission[0], tokenizer(tokens))
    except Exception as exc:                       # noqa: BLE001
        log.warning("Could not align words (%s); falling back to sentences", exc)
        return []

    if len(spans) != len(tokens):
        log.warning("Aligner returned %d spans for %d words; skipping",
                    len(spans), len(tokens))
        return []

    # Emission frames are a fixed downsample of the waveform; this converts a
    # frame index back to seconds in the original clip.
    per_frame = resampled.shape[1] / emission.shape[1] / bundle.sample_rate
    clip_seconds = resampled.shape[1] / bundle.sample_rate

    timed: list[dict] = []
    spans_iter = iter(spans)
    for original, clean in pieces:
        if clean:
            span = next(spans_iter)
            timed.append({"text": original,
                          "start": span[0].start * per_frame,
                          "end": span[-1].end * per_frame})
        else:
            timed.append({"text": original, "start": None, "end": None})

    # An untimed word takes the gap its neighbours left — which is where its
    # audio actually is, since the aligner had to skip over it. "chapters 2-6"
    # otherwise leaves "chapters" holding two and a half seconds while the
    # numbers are read, and the highlight sits still through them.
    for i, word in enumerate(timed):
        if word["start"] is not None:
            continue
        before = next((timed[j]["end"] for j in range(i - 1, -1, -1)
                       if timed[j]["end"] is not None), 0.0)
        after = next((timed[j]["start"] for j in range(i + 1, len(timed))
                      if timed[j]["start"] is not None), clip_seconds)
        word["start"], word["end"] = before, max(before, after)

    for word in timed:
        word["start"] = round(word["start"], 3)
        word["end"] = round(word["end"], 3)
    return timed
