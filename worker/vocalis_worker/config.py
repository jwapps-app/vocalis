import os
from pathlib import Path

# No password in the default: libpq reads PGPASSWORD from the environment, and
# install.sh puts it there. A working password committed as a fallback is a
# password everyone deploying this shares without noticing.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://vocalis@127.0.0.1:5445/vocalis"
)

# Where the API lives. Books and finished audio move over HTTP, so this is the
# only thing besides the database the worker needs to know about the server —
# and in particular there is no shared directory to mount or keep in step.
API_URL = os.environ.get("VOCALIS_API_URL", "http://127.0.0.1:8091")

# Purely local scratch: downloaded books, chapter audio, the assembled file
# before it is uploaded. Nothing here is shared with the API, so it belongs on
# fast local disk rather than on a share.
WORK_DIR = Path(
    os.environ.get("VOCALIS_WORK_DIR", "~/Library/Application Support/Vocalis")
).expanduser().resolve()

POLL_INTERVAL_SECONDS = float(os.environ.get("VOCALIS_POLL_INTERVAL", "5"))

# Max characters per TTS chunk. Two forces set this: Chatterbox degrades on
# very long inputs, and a chunk's peak GPU memory scales with the audio
# sequence it generates — long chunks are what pushed a process past its memory
# budget mid-book. 300 keeps peak per chunk well clear of the cap; sentences
# are the split points, so the extra boundaries fall where the voice pauses.
MAX_CHUNK_CHARS = int(os.environ.get("VOCALIS_MAX_CHUNK_CHARS", "300"))

# Chunks per render task. A chapter is split into segments of this size so that
# process lifetime is bounded by *chunks*, not chapters — Metal's graph cache
# grows with every distinct chunk shape and only a process restart clears it,
# so one very long chapter must not be a single indivisible task. Measured
# growth is ~0.13 GB per chunk against ~9.5 GB of headroom, i.e. roughly 73
# chunks; 20 per segment with a couple of segments per process stays well clear.
SEGMENT_CHUNKS = int(os.environ.get("VOCALIS_SEGMENT_CHUNKS", "20"))
