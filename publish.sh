#!/bin/bash
# Relance le scraper, puis publie les profils sur GitHub Pages
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
PROFILE="${1:-all}"
cd "$SCRIPT_DIR"

"$VENV" scraper.py --profile "$PROFILE"

if [ "$PROFILE" = "all" ]; then
  git add docs/index.html docs/status.html docs/assets docs/icon.svg docs/manifest.webmanifest docs/sw.js \
          docs/lettres/index.html docs/lettres/feed.xml \
          docs/comptabilite/index.html docs/comptabilite/feed.xml \
          docs/systemes/index.html docs/systemes/feed.xml
else
  git add docs/index.html docs/status.html docs/assets docs/icon.svg docs/manifest.webmanifest docs/sw.js \
          "docs/$PROFILE/index.html" "docs/$PROFILE/feed.xml"
fi
git commit -m "Mise à jour des offres d'emploi $(date '+%Y-%m-%d %H:%M')"
git push

echo ""
echo "Publié : https://gabigbarig.github.io/find_job/"
