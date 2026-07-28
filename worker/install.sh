#!/bin/sh
# Vocalis narrator — installer for the machine with the GPU.
#
# Downloaded as part of the worker bundle from the Vocalis web UI, which also
# writes the .env sitting next to this script with the address of your Vocalis
# server. Run it from inside the unpacked bundle:
#
#   ./install.sh
#
# It creates a Python environment, installs the TTS stack, and registers the
# narrator as a background service so it starts with the machine.

set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}
VENV="$HERE/worker/.venv"
LABEL=com.jwapps.vocalis.worker

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }

[ -f "$HERE/.env" ] || die "no .env beside this script — re-download the bundle from Vocalis."
# shellcheck disable=SC1091
. "$HERE/.env"
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL missing from .env"

# The bundle ships without the database password: it is downloaded over an
# unauthenticated connection, so anything in it is readable by anyone who can
# reach the server. Ask for it here instead — it is the POSTGRES_PASSWORD from
# the server's .env. Carried as PGPASSWORD, which libpq reads by itself, so it
# never has to be URL-encoded into a connection string.
# Read from the terminal, not stdin. Run as `curl … | sh`, stdin is the pipe
# carrying this script and is already at end-of-file, so a plain `read` returns
# nothing at once — the prompt appears and the installer exits in the same
# breath, looking like it ignored you.
#
# Opening /dev/tty is attempted rather than tested for: `[ -r /dev/tty ]` is
# true even where the device cannot actually be opened, which is the case
# anywhere without a controlling terminal.
DB_PASSWORD=""
if { exec 3<>/dev/tty; } 2>/dev/null; then
  printf 'Database password (POSTGRES_PASSWORD from the Vocalis server): ' >&3
  if stty -echo <&3 2>/dev/null; then
    read -r DB_PASSWORD <&3
    stty echo <&3 2>/dev/null
    printf '\n' >&3
  else
    read -r DB_PASSWORD <&3
  fi
  exec 3<&-
fi

# No terminal — a CI run, or both ends piped. Take it from the environment
# rather than hanging on input nobody can supply.
[ -n "$DB_PASSWORD" ] || DB_PASSWORD=${POSTGRES_PASSWORD:-}
[ -n "$DB_PASSWORD" ] || die "no database password.

Run this from a terminal, or set POSTGRES_PASSWORD in the environment first."

# Books and finished audio move over HTTP, so this is the only other thing the
# worker needs to know. The bundle names it; fall back to the database host,
# which is the same machine in every layout this installs.
if [ -z "${VOCALIS_API_URL:-}" ]; then
  db_host=$(printf '%s' "$DATABASE_URL" | sed -e 's|.*@||' -e 's|/.*||' -e 's|:.*||')
  VOCALIS_API_URL="http://$db_host:8091"
fi

case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *) die "unsupported system $(uname -s). See the setup page for Windows." ;;
esac

# ------------------------------------------------------------------- python
#
# PyTorch and Chatterbox lag new Python releases, and the system python3 is
# routinely too new — 3.13 on a current Mac, where the install fails partway
# through with a compiler error rather than anything about versions. Pick a
# version known to work, from wherever one already exists, and fetch one only
# if none does.
supported() {
  # Lower bound is core/pyproject.toml's requires-python, not a guess: accepting
  # 3.10 meant the venv was built and PyTorch downloaded before pip refused
  # vocalis-core, several minutes in and with a message about a package rather
  # than about the interpreter. Upper bound is what PyTorch supports.
  "$1" -c 'import sys; raise SystemExit(0 if (3,11) <= sys.version_info < (3,13) else 1)' 2>/dev/null
}

find_python() {
  [ -n "${PYTHON:-}" ] && { printf '%s' "$PYTHON"; return; }
  for candidate in python3.12 python3.11 python3; do
    full=$(command -v "$candidate" 2>/dev/null) || continue
    supported "$full" && { printf '%s' "$full"; return; }
  done
  # uv keeps interpreters outside PATH; a matching one is often already there.
  for full in "$HOME"/.local/share/uv/python/cpython-3.1[12]*/bin/python3.1[12]; do
    [ -x "$full" ] && supported "$full" && { printf '%s' "$full"; return; }
  done
  printf ''
}

PY=$(find_python)
if [ -z "$PY" ]; then
  say "Fetching a suitable Python (the system one is too new for PyTorch)"
  if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 \
      || die "could not install uv, needed to fetch Python 3.12. Install Python 3.12 yourself and re-run."
    # shellcheck disable=SC1090
    [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
    PATH="$HOME/.local/bin:$PATH"
  fi
  uv python install 3.12 >/dev/null 2>&1 || die "could not fetch Python 3.12"
  PY=$(find_python)
  [ -n "$PY" ] || die "fetched Python 3.12 but cannot find it — install Python 3.12 yourself and re-run."
fi
say "Using $("$PY" --version 2>&1) at $PY"

if ! command -v ffmpeg >/dev/null; then
  if [ "$OS" = mac ] && command -v brew >/dev/null; then
    say "Installing ffmpeg"
    brew install ffmpeg >/dev/null 2>&1 || die "ffmpeg install failed. Run: brew install ffmpeg"
  else
    die "ffmpeg not found. macOS: brew install ffmpeg. Linux: apt install ffmpeg."
  fi
fi

say "1/4  Creating the Python environment"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

say "2/4  Installing the narrator (this downloads PyTorch — several minutes)"
"$VENV/bin/pip" install --quiet -r "$HERE/worker/requirements.txt"
"$VENV/bin/pip" install --quiet -e "$HERE/core"

say "3/4  Registering the background service"
if [ "$OS" = mac ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  # The file is about to hold the database password: create it unreadable by
  # anyone else before writing, rather than fixing the mode afterwards.
  : > "$PLIST"
  chmod 600 "$PLIST"
  cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/python</string>
        <string>-m</string>
        <string>vocalis_worker.main</string>
    </array>
    <key>WorkingDirectory</key><string>$HERE/worker</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DATABASE_URL</key><string>$DATABASE_URL</string>
        <key>PGPASSWORD</key><string>$DB_PASSWORD</string>
        <key>VOCALIS_API_URL</key><string>$VOCALIS_API_URL</string>
        <key>VOCALIS_WORKER_TOKEN</key><string>${VOCALIS_WORKER_TOKEN:-}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/vocalis-worker.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/vocalis-worker.log</string>
</dict>
</plist>
PLIST_END
  # bootout returns before launchd has finished tearing the old service down,
  # and bootstrapping into that window fails with "Input/output error" — which
  # says nothing about the actual cause. Wait for it to go, then retry.
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  attempt=0
  until launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; do
    attempt=$((attempt + 1))
    [ "$attempt" -ge 10 ] && die "could not register the service. Try: launchctl bootstrap gui/$(id -u) $PLIST"
    sleep 1
  done
  LOG="$HOME/Library/Logs/vocalis-worker.log"
  STOP="launchctl bootout gui/\$(id -u)/$LABEL"
  RESTART="launchctl kickstart -k gui/\$(id -u)/$LABEL"
else
  UNIT="$HOME/.config/systemd/user/vocalis-worker.service"
  mkdir -p "$(dirname "$UNIT")"
  # Holds the database password — keep it to this user.
  : > "$UNIT"
  chmod 600 "$UNIT"
  cat > "$UNIT" <<UNIT_END
[Unit]
Description=Vocalis narrator
After=network-online.target

[Service]
ExecStart=$VENV/bin/python -m vocalis_worker.main
WorkingDirectory=$HERE/worker
Environment=DATABASE_URL=$DATABASE_URL
Environment=PGPASSWORD=$DB_PASSWORD
Environment=VOCALIS_API_URL=$VOCALIS_API_URL
Environment=VOCALIS_WORKER_TOKEN=${VOCALIS_WORKER_TOKEN:-}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT_END
  systemctl --user daemon-reload
  systemctl --user enable --now vocalis-worker.service
  LOG="journalctl --user -u vocalis-worker -f"
  STOP="systemctl --user stop vocalis-worker"
  RESTART="systemctl --user restart vocalis-worker"
fi

say "4/4  Done"
cat <<DONE
The narrator is running and will start automatically from now on. The first
book downloads the voice model from Hugging Face, which takes a few minutes.

  Vocalis should now show the narrator as connected on its setup page.

  Logs     $LOG
  Restart  $RESTART
  Stop     $STOP
DONE
