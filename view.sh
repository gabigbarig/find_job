#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/python3"
PROFILE="${1:-all}"
cd "$SCRIPT_DIR"
"$VENV" scraper.py --profile "$PROFILE"
if [ "$PROFILE" = "all" ]; then
  explorer.exe "$(wslpath -w "$SCRIPT_DIR/docs/index.html")"
else
  explorer.exe "$(wslpath -w "$SCRIPT_DIR/data/$PROFILE/results.html")"
fi
