#!/usr/bin/env python3
"""Render a short Chatterbox preview for every narrator missing one.

Previews are what the web UI's play button serves. They are rendered through
Chatterbox (with each narrator's reference clip and params), so they sound
exactly like a converted book would — not like the raw reference clip.

    .venv/bin/python make_previews.py [--force]

Output: <DATA_DIR>/narrators/previews/<id>.wav
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vocalis_worker.config import DATA_DIR  # noqa: E402
from vocalis_worker.synth import Synthesizer  # noqa: E402

PREVIEW_TEXT = (
    "The rain had stopped by the time she reached the harbor, "
    "and the city lights trembled on the water like distant fires."
)
SEED = 1234

# Mirrors the built-in presets in api/app/narrators.py (currently none).
BUILTIN: list[dict] = []


def narrators() -> list[dict]:
    entries = list(BUILTIN)
    manifest = DATA_DIR / "narrators" / "manifest.json"
    if manifest.is_file():
        entries += json.loads(manifest.read_text()).get("narrators", [])
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-render existing previews")
    args = ap.parse_args()

    preview_dir = DATA_DIR / "narrators" / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    todo = [
        n for n in narrators()
        if args.force or not (preview_dir / f"{n['id']}.wav").is_file()
    ]
    if not todo:
        print("all previews present")
        return

    synth = Synthesizer()
    for n in todo:
        ref = DATA_DIR / n["ref"] if n.get("ref") else None
        out = preview_dir / f"{n['id']}.wav"
        print(f"rendering {n['id']} ...")
        duration = synth.synth_chapter(
            [PREVIEW_TEXT], ref, SEED, out, params=n.get("params") or {}
        )
        print(f"  {out.name}: {duration:.1f}s")


if __name__ == "__main__":
    main()
