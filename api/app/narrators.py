"""Narrator presets.

A narrator is a display name plus how to make Chatterbox sound like them:
an optional reference voice clip (zero-shot cloning) and/or generation
params (exaggeration, cfg_weight).

Built-in narrators use Chatterbox's stock voice at different delivery
settings, so they work with no extra files. Additional narrators — e.g.
clips of public-domain LibriVox readers — are loaded from
<DATA_DIR>/narrators/manifest.json:

    {"narrators": [{"id": "...", "name": "...", "description": "...",
                    "ref": "narrators/<id>.wav",
                    "params": {"exaggeration": 0.5, "cfg_weight": 0.5}}]}
"""

import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MANIFEST = DATA_DIR / "narrators" / "manifest.json"

# Voices come from the manifest (Kokoro-derived clips, user-added presets).
# Chatterbox's stock "Nova" voice was removed from the menu by request; an
# entry with "ref": null would bring it back.
BUILTIN: list[dict] = []


def _from_manifest() -> list[dict]:
    if not MANIFEST.is_file():
        return []
    try:
        entries = json.loads(MANIFEST.read_text()).get("narrators", [])
    except (json.JSONDecodeError, OSError):
        return []
    valid = []
    for entry in entries:
        if not entry.get("id") or not entry.get("name"):
            continue
        ref = entry.get("ref")
        if ref and not (DATA_DIR / ref).is_file():
            continue  # clip missing — hide rather than fail jobs later
        valid.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "description": entry.get("description", ""),
                "ref": ref,
                "params": entry.get("params", {}),
            }
        )
    return valid


def list_narrators() -> list[dict]:
    builtin_ids = {n["id"] for n in BUILTIN}
    extra = [n for n in _from_manifest() if n["id"] not in builtin_ids]
    return BUILTIN + extra


def resolve(narrator_id: str) -> dict | None:
    for narrator in list_narrators():
        if narrator["id"] == narrator_id:
            return narrator
    return None
