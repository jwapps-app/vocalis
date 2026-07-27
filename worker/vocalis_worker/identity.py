"""What this machine is, for the setup page's "narrator connected" panel.

Deliberately torch-free in the long-lived parent process. Importing torch here
would add a gigabyte to a process that exists only to poll Postgres and shepherd
the render pool — the whole point of the pool is that torch lives in the
children. The device probe therefore runs once, in a throwaway subprocess, and
the answer is cached for the life of the worker.
"""

import json
import logging
import os
import platform
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_PROBE = """
import json, platform, subprocess, torch

def mac_chip():
    # Absolute path: sysctl lives in /usr/sbin, which is not on the minimal
    # PATH a launchd service inherits, so a bare 'sysctl' is not found there.
    try:
        return subprocess.run(
            ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return platform.machine()

GB = 1024 ** 3


def free_mps_gb():
    # Metal exposes no device-wide "free memory" query, and the number that
    # matters is not this process's usage but what is left after every other
    # app — the OOM message's "other allocations". So find it empirically:
    # allocate under the same ceiling the renderers use until Metal refuses,
    # then hand it all back. The answer is real headroom, right now.
    blocks, step = [], 0.5
    try:
        while len(blocks) < 120:
            blocks.append(torch.empty(int(step * GB // 4), dtype=torch.float32,
                                      device="mps"))
    except RuntimeError:
        pass
    free = len(blocks) * step
    del blocks
    torch.mps.empty_cache()
    return free


out = {"device": "cpu", "device_name": platform.processor() or platform.machine(),
       "free_gpu_gb": None}
if torch.backends.mps.is_available():
    out["device"] = "mps"
    out["device_name"] = mac_chip()          # e.g. 'Apple M4 Pro'
    out["free_gpu_gb"] = free_mps_gb()
elif torch.cuda.is_available():
    out["device"] = "cuda"
    out["device_name"] = torch.cuda.get_device_name(0)
    # Discrete GPUs report this directly — no probing needed.
    out["free_gpu_gb"] = torch.cuda.mem_get_info()[0] / GB
print(json.dumps(out))
"""

_cached: dict | None = None


def worker_id() -> str:
    """Stable per-installation token, so restarts reuse one `workers` row."""
    path = config.DATA_DIR / ".worker-id"
    try:
        return path.read_text().strip()
    except OSError:
        token = uuid.uuid4().hex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token)
        except OSError:
            log.warning("Could not persist worker id under %s", path.parent)
        return token


def _probe_device() -> dict:
    from .pool import _mps_env

    try:
        out = subprocess.run(
            [sys.executable, "-c", _PROBE],
            capture_output=True, text=True, timeout=300, check=True,
            # Probe under the renderers' own ceiling, or it would measure
            # headroom the renderers are never allowed to use.
            env={**os.environ, **_mps_env(1)},
        )
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:
        log.warning("Could not determine the GPU device (%s)", exc)
        return {"device": "unknown", "device_name": platform.machine(),
                "free_gpu_gb": None}


def refresh() -> dict:
    """Re-measure GPU headroom and recompute how many chapters fit.

    Worth doing per job rather than once at startup: free GPU is whatever the
    rest of the desktop leaves behind, and that changes over hours as browsers
    and editors come and go. A measurement taken at login says nothing about
    conditions when a book actually starts.
    """
    global _cached
    from .pool import safe_concurrency

    probe = _probe_device()
    _cached = {
        "id": worker_id(),
        "hostname": socket.gethostname(),
        "version": platform.platform(terse=True),
        **probe,
        "max_concurrency": safe_concurrency(99, probe.get("free_gpu_gb")),
    }
    free = _cached.get("free_gpu_gb")
    log.info(
        "Narrating on %s (%s); %s GPU free — up to %d chapter(s) at once",
        _cached["device"], _cached["device_name"],
        f"{free:.1f} GB" if free is not None else "unknown",
        _cached["max_concurrency"],
    )
    return _cached


def describe() -> dict:
    """Identity and hardware of this worker. Measures once, then caches."""
    global _cached
    if _cached is None:
        refresh()
    return _cached


