#!/bin/bash
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
PROFILE="${1:-all}"
LOCK_FILE="${TMPDIR:-/tmp}/find_job-scraper-$(id -u).lock"
cd "$SCRIPT_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Une recherche d'emploi est déjà en cours. Réessaie après sa fin." >&2
  exit 1
fi

"$VENV" scraper.py --profile "$PROFILE"
if [ "$PROFILE" = "all" ]; then
  explorer.exe "$(wslpath -w "$SCRIPT_DIR/docs/index.html")"
else
  explorer.exe "$(wslpath -w "$SCRIPT_DIR/data/$PROFILE/results.html")"
fi
