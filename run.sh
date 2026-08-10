#!/bin/bash
# Lance le scraper avec le bon environnement Python
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
PROFILE="${1:-all}"
LOCK_FILE="${TMPDIR:-/tmp}/find_job-scraper-$(id -u).lock"
cd "$SCRIPT_DIR"
mkdir -p data

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Une recherche d'emploi est déjà en cours. Nouvelle exécution ignorée." >> data/scraper.log
  exit 0
fi

FIND_JOB_LOCK_HELD=1 "$VENV" scraper.py --profile "$PROFILE" >> data/scraper.log 2>&1
