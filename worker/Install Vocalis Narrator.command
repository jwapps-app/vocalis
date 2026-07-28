#!/bin/sh
# Double-clickable wrapper around install.sh.
#
# Finder runs a .command file in Terminal, which install.sh needs: it asks for
# the database password and reports progress. The only thing this adds is
# running from the right directory and holding the window open at the end, so
# a failure is readable instead of vanishing with the window.
cd "$(dirname "$0")" || exit 1
./install.sh
status=$?
printf '\n'
if [ $status -eq 0 ]; then
  printf 'Done. You can close this window.\n'
else
  printf 'Install failed (exit %s). The message above says why.\n' "$status"
fi
printf 'Press Return to close. '
read -r _
