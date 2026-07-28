"""Files in and out over HTTP, so the worker shares no filesystem with the API.

The worker used to read the uploaded book and write finished audio straight
into a directory the API also had open. On one machine that is invisible; split
across two it means a network share, matching paths at both ends, credentials
in a keychain, and something to remount the share after a reboot — every one of
which is a way for an install to fail late and obscurely, with a job that
starts and then cannot find its book.

Fetching what it needs and posting back what it made leaves the two halves
sharing a database and nothing else. The scratch audio stays on local disk,
which is both simpler and faster than writing a few hundred megabytes an hour
across SMB.

urllib rather than requests: the worker's dependencies are already heavy with
torch, and this is a handful of calls against a server on the same network.
"""

import json
import logging
import mimetypes
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

TIMEOUT = 120


class TransportError(RuntimeError):
    """A request to the API failed in a way worth reporting on the job."""


def _url(path: str) -> str:
    return f"{config.API_URL.rstrip('/')}{path}"


def _headers() -> dict:
    """The narrator's credential. It cannot log in — it runs unattended on
    another machine — so it presents a token the server issued and shipped
    inside the worker bundle, which is itself behind the password."""
    return {"X-Vocalis-Worker": config.WORKER_TOKEN} if config.WORKER_TOKEN else {}


def download(path: str, dest: Path) -> bool:
    """Fetch a file to `dest`. False if the server says it does not exist.

    Writes through a temporary name so an interrupted download cannot leave a
    truncated file that later looks like a complete one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        request = urllib.request.Request(_url(path), headers=_headers())
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            tmp.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code == 404:
            return False
        raise TransportError(f"GET {path} failed: {exc.code} {exc.reason}") from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise TransportError(f"GET {path} failed: {exc}") from exc
    tmp.replace(dest)
    return True


def upload(path: str, source: Path, field: str = "file") -> dict:
    """POST a file as multipart/form-data and return the JSON response."""
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; '
        f'filename="{source.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        source.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        _url(path), data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 **_headers()},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise TransportError(
            f"POST {path} failed: {exc.code} {exc.reason}"
        ) from exc
    except OSError as exc:
        raise TransportError(f"POST {path} failed: {exc}") from exc


def reachable() -> bool:
    try:
        probe = urllib.request.Request(_url("/api/narrators"), headers=_headers())
        with urllib.request.urlopen(probe, timeout=10):
            return True
    except Exception:
        return False
