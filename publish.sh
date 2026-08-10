#!/bin/bash
# Relance le scraper, puis publie les profils sur GitHub Pages
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
PROFILE="${1:-all}"
LOCK_FILE="${TMPDIR:-/tmp}/find_job-scraper-$(id -u).lock"
cd "$SCRIPT_DIR"

# Empêche le cron et une publication manuelle d'écrire simultanément dans data/ et docs/.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Une recherche d'emploi est déjà en cours. Réessaie après sa fin." >&2
  exit 1
fi

FIND_JOB_LOCK_HELD=1 "$VENV" scraper.py --profile "$PROFILE"

if [ "$PROFILE" = "all" ]; then
  git add docs/index.html docs/status.html docs/assets docs/icon.svg docs/manifest.webmanifest docs/sw.js \
          docs/lettres/index.html docs/lettres/feed.xml \
          docs/comptabilite/index.html docs/comptabilite/feed.xml \
          docs/systemes/index.html docs/systemes/feed.xml
else
  git add docs/index.html docs/status.html docs/assets docs/icon.svg docs/manifest.webmanifest docs/sw.js \
          "docs/$PROFILE/index.html" "docs/$PROFILE/feed.xml"
fi

if git diff --cached --quiet; then
  echo "Aucune modification de rapport à valider."
else
  git commit -m "Mise à jour des offres d'emploi $(date '+%Y-%m-%d %H:%M')"
fi

# GitHub Actions peut avoir publié pendant le scraping. Rebase les commits locaux
# sur la version distante ; en cas de conflit sur les rapports générés, conserve
# la version locale qui vient d'être produite.
git fetch origin main
git rebase --autostash -X theirs origin/main
if ! git push origin main; then
  echo "Le dépôt distant a encore changé. Nouvelle synchronisation avant un dernier essai..."
  git fetch origin main
  git rebase --autostash -X theirs origin/main
  git push origin main
fi

echo ""
echo "Publié : https://gabigbarig.github.io/find_job/"
