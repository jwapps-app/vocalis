#!/usr/bin/env python3
"""Add a narrator preset from an audio file or URL.

Downloads/copies the source audio, trims a clean reference segment, converts
it to 24 kHz mono WAV in <DATA_DIR>/narrators/, and registers it in
manifest.json so it appears in the web UI dropdown.

    python add_narrator.py ruth-golding "Ruth Golding" \
        https://archive.org/download/<item>/<file>.mp3 \
        --start 45 --duration 20 \
        --description "LibriVox narrator (public-domain recordings)"

Only use voices you have rights to: your own recordings, public-domain
sources whose readers released them (e.g. LibriVox), or voices with explicit
consent. Do not clone commercial audiobook narrators.
"""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vocalis_worker.config import DATA_DIR  # noqa: E402

NARRATOR_DIR = DATA_DIR / "narrators"
MANIFEST = NARRATOR_DIR / "manifest.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("id", help="slug, e.g. ruth-golding")
    ap.add_argument("name", help="display name for the dropdown")
    ap.add_argument("source", help="local audio file or http(s) URL")
    ap.add_argument("--start", type=float, default=0, help="trim start (seconds)")
    ap.add_argument("--duration", type=float, default=20, help="clip length (seconds, 5-30 ideal)")
    ap.add_argument("--description", default="")
    ap.add_argument("--exaggeration", type=float, default=None)
    ap.add_argument("--cfg-weight", type=float, default=None)
    args = ap.parse_args()

    NARRATOR_DIR.mkdir(parents=True, exist_ok=True)

    if args.source.startswith(("http://", "https://")):
        raw = NARRATOR_DIR / f"{args.id}.src"
        print(f"downloading {args.source} ...")
        urllib.request.urlretrieve(args.source, raw)
    else:
        raw = Path(args.source)
        if not raw.is_file():
            sys.exit(f"no such file: {raw}")

    wav = NARRATOR_DIR / f"{args.id}.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-ss", str(args.start), "-t", str(args.duration),
         "-i", str(raw), "-ac", "1", "-ar", "24000", str(wav)],
        check=True, capture_output=True,
    )
    if raw.suffix == ".src":
        raw.unlink()

    params = {}
    if args.exaggeration is not None:
        params["exaggeration"] = args.exaggeration
    if args.cfg_weight is not None:
        params["cfg_weight"] = args.cfg_weight

    manifest = {"narrators": []}
    if MANIFEST.is_file():
        manifest = json.loads(MANIFEST.read_text())
    manifest["narrators"] = [n for n in manifest.get("narrators", []) if n.get("id") != args.id]
    manifest["narrators"].append(
        {
            "id": args.id,
            "name": args.name,
            "description": args.description,
            "ref": f"narrators/{args.id}.wav",
            "params": params,
        }
    )
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"added narrator {args.name!r} -> {wav}")
    print("it will appear in the web UI dropdown immediately (no restart needed)")


if __name__ == "__main__":
    main()
