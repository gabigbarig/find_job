#!/bin/bash
# Relance le scraper, puis publie docs/index.html sur GitHub Pages
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
cd "$SCRIPT_DIR"

"$VENV" scraper.py

git add docs/index.html
git commit -m "Mise à jour des offres d'emploi $(date '+%Y-%m-%d %H:%M')"
git push

echo ""
echo "Publié : https://gabigbarig.github.io/find_job/"
