#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
cd "$SCRIPT_DIR"
"$VENV" scraper.py
explorer.exe "$(wslpath -w "$SCRIPT_DIR/data/results.html")"
