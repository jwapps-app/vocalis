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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def check_base_url() -> None:
    """Warn at startup if the API address redirects somewhere else.

    A redirect is survivable for the small requests — urllib follows it — and
    fatal for the one that matters. Posting a finished audiobook means writing
    a hundred megabytes; the server answers 301 and closes the connection long
    before that finishes, and the error surfaces as "Broken pipe" with nothing
    naming the cause. Hours of narration then fail at the last step, and the
    address in the log looks perfectly correct.

    That is exactly what a proxy in front of a site does with http:// when the
    site is served over TLS. Cheaper to say so once at startup than to let it
    be discovered at the end of a book.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(_url("/api/auth/status"), headers=_headers())
    try:
        opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        target = exc.headers.get("Location") if exc.headers else None
        if exc.code in (301, 302, 307, 308) and target:
            base = target.split("/api/")[0] or target
            log.error(
                "%s redirects to %s. Uploading a finished book will fail there."
                " Set VOCALIS_API_URL to %s and restart.",
                config.API_URL, target, base,
            )
    except OSError as exc:
        log.warning("Could not reach %s at startup (%s)", config.API_URL, exc)


# urllib announces itself as "Python-urllib/3.x", which sits on the default
# block list of every bot-protection service there is. Cloudflare returns 403
# to it — and on an upload it returns that 403 and closes the connection while
# the narrator is still writing a hundred megabytes, so an entire book dies at
# the last step reporting "Broken pipe", with nothing naming a user agent.
#
# Saying who we are costs one header and makes the narrator work through the
# proxy people put a self-hosted service behind.
USER_AGENT = "Vocalis-Narrator/1.0 (+https://github.com/jwapps-app/vocalis)"


def _headers() -> dict:
    """The narrator's credential, and a name for the proxies in between.

    It cannot log in — it runs unattended on another machine — so it presents a
    token the server issued and shipped inside the worker bundle, which is
    itself behind the password.
    """
    headers = {"User-Agent": USER_AGENT}
    if config.WORKER_TOKEN:
        headers["X-Vocalis-Worker"] = config.WORKER_TOKEN
    return headers


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
