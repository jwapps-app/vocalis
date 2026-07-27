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
[ -n "${VOCALIS_DATA_DIR:-}" ] || VOCALIS_DATA_DIR="$HERE/data"

case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *) die "unsupported system $(uname -s). See the setup page for Windows." ;;
esac

command -v "$PY" >/dev/null || die "$PY not found. Install Python 3.11 or newer."
command -v ffmpeg >/dev/null || die "ffmpeg not found. macOS: brew install ffmpeg. Linux: apt install ffmpeg."

say "1/4  Creating the Python environment"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

say "2/4  Installing the narrator (this downloads PyTorch — several minutes)"
"$VENV/bin/pip" install --quiet -r "$HERE/worker/requirements.txt"
"$VENV/bin/pip" install --quiet -e "$HERE/core"

mkdir -p "$VOCALIS_DATA_DIR"

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
