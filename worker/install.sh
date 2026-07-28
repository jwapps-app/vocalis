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
printf 'Database password (POSTGRES_PASSWORD from the Vocalis server): '
if stty -echo 2>/dev/null; then
  read -r DB_PASSWORD; stty echo; printf '\n'
else
  read -r DB_PASSWORD
fi
[ -n "$DB_PASSWORD" ] || die "no password entered"

case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *) die "unsupported system $(uname -s). See the setup page for Windows." ;;
esac

# ---------------------------------------------------------------- data folder
#
# The narrator reads the uploaded book and writes chapter audio back, so it
# needs the *same* folder the server uses — not a folder of its own. Asking for
# a path invites a plausible wrong answer that only surfaces later as a job
# failing to find its book, so work it out instead and verify it.
find_data_dir() {
  # Told explicitly: trust it.
  if [ -n "${VOCALIS_DATA_DIR:-}" ]; then
    printf '%s' "$VOCALIS_DATA_DIR"; return
  fi
  server_dir=${VOCALIS_SERVER_DATA_DIR:-}
  # Same machine as the server: the server's own path is already correct.
  if [ -n "$server_dir" ] && [ -d "$server_dir/narrators" ]; then
    printf '%s' "$server_dir"; return
  fi
  # A different machine: the share is mounted somewhere, with the tail of the
  # server's path below it. Recognise it by the narrator voices the server
  # seeded, which no other folder on the machine will have.
  for base in /Volumes/* /mnt/* /media/*/*; do
    [ -d "$base" ] || continue
    if [ -n "$server_dir" ]; then
      candidate="$base/${server_dir#/}"
      [ -d "$candidate/narrators" ] && { printf '%s' "$candidate"; return; }
    fi
    for hit in $(find "$base" -maxdepth 4 -type d -name narrators 2>/dev/null); do
      [ -f "$hit/manifest.json" ] && { printf '%s' "${hit%/narrators}"; return; }
    done
  done
  printf ''
}

VOCALIS_DATA_DIR=$(find_data_dir)
if [ -z "$VOCALIS_DATA_DIR" ]; then
  printf '\n'
  die "cannot find the Vocalis data folder.

The narrator shares it with the server${VOCALIS_SERVER_DATA_DIR:+, which keeps it at
  $VOCALIS_SERVER_DATA_DIR}.

If the server is on another machine, mount its share first — on a Mac,
Finder > Go > Connect to Server — then run this again. Nothing else needs
setting up; it is found automatically once mounted."
fi
say "Sharing the server's data folder at $VOCALIS_DATA_DIR"

# ------------------------------------------------------------------- python
#
# PyTorch and Chatterbox lag new Python releases, and the system python3 is
# routinely too new — 3.13 on a current Mac, where the install fails partway
# through with a compiler error rather than anything about versions. Pick a
# version known to work, from wherever one already exists, and fetch one only
# if none does.
supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3,10) <= sys.version_info < (3,13) else 1)' 2>/dev/null
}

find_python() {
  [ -n "${PYTHON:-}" ] && { printf '%s' "$PYTHON"; return; }
  for candidate in python3.12 python3.11 python3.10 python3; do
    full=$(command -v "$candidate" 2>/dev/null) || continue
    supported "$full" && { printf '%s' "$full"; return; }
  done
  # uv keeps interpreters outside PATH; a matching one is often already there.
  for full in "$HOME"/.local/share/uv/python/cpython-3.1[012]*/bin/python3.1[012]; do
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

# Deliberately not `mkdir -p "$VOCALIS_DATA_DIR"`: if the share is a network
# mount that is currently absent, creating it would make an empty local folder
# that shadows the mount point, and the worker would fill the startup disk with
# audio nobody can find. find_data_dir already proved the folder exists.

# --------------------------------------------------- keep a share mounted
#
# macOS does not remount an SMB or NFS share after a restart, so a narrator set
# to start at login would come back to an empty mount point. Register a login
# agent that mounts it first, using the credentials already saved in the
# keychain by the original Finder connection.
mount_agent() {
  [ "$OS" = mac ] || return 0
  case "$VOCALIS_DATA_DIR" in /Volumes/*) ;; *) return 0 ;; esac
  volume="/Volumes/$(printf '%s' "${VOCALIS_DATA_DIR#/Volumes/}" | cut -d/ -f1)"
  # e.g. "//user@host/share on /Volumes/share (smbfs, ...)"
  source=$(mount | awk -v v="$volume" '$3 == v { print $1; exit }')
  case "$source" in //*) ;; *) return 0 ;; esac   # only handles SMB
  url="smb:$(printf '%s' "$source" | sed 's/ /%20/g')"

  MOUNT_LABEL="$LABEL.mount"
  MOUNT_PLIST="$HOME/Library/LaunchAgents/$MOUNT_LABEL.plist"
  cat > "$MOUNT_PLIST" <<MOUNT_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$MOUNT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>[ -d "$VOCALIS_DATA_DIR/narrators" ] || /usr/bin/open "$url"</string>
    </array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
MOUNT_END
  launchctl bootout "gui/$(id -u)/$MOUNT_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$MOUNT_PLIST" 2>/dev/null || true
  say "Registered a login agent to remount $volume after a restart"
}
mount_agent

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
        <key>VOCALIS_DATA_DIR</key><string>$VOCALIS_DATA_DIR</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/vocalis-worker.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/vocalis-worker.log</string>
</dict>
</plist>
PLIST_END
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
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
Environment=VOCALIS_DATA_DIR=$VOCALIS_DATA_DIR
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
