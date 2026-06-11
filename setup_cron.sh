#!/bin/bash
# Installe les tâches cron pour lancer le scraper à 8h et 18h chaque jour

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run.sh"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
SCRAPER="$SCRIPT_DIR/scraper.py"

# Crée le répertoire de données si absent
mkdir -p "$SCRIPT_DIR/data"

# Assure que run.sh est exécutable
chmod +x "$RUN_SCRIPT"

# Construit les lignes cron (8h et 18h tous les jours)
CRON_8H="0 8 * * * $VENV_PYTHON $SCRAPER >> $SCRIPT_DIR/data/scraper.log 2>&1"
CRON_18H="0 18 * * * $VENV_PYTHON $SCRAPER >> $SCRIPT_DIR/data/scraper.log 2>&1"

# Ajoute les entrées cron sans dupliquer
(crontab -l 2>/dev/null | grep -v "find_job.*scraper.py"; echo "$CRON_8H"; echo "$CRON_18H") | crontab -

echo "Cron configuré :"
crontab -l | grep scraper.py
echo ""
echo "Le scraper tournera tous les jours à 8h00 et 18h00."
echo "Les résultats sont dans : $SCRIPT_DIR/data/results.html"
echo "Les logs sont dans      : $SCRIPT_DIR/data/scraper.log"
