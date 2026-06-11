#!/bin/bash
# Lance le scraper avec le bon environnement Python
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
cd "$SCRIPT_DIR"
mkdir -p data
"$VENV" scraper.py >> data/scraper.log 2>&1
