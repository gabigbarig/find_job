#!/bin/bash
# Installe les tâches cron pour lancer le scraper à 8h et 18h chaque jour
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_SCRIPT="$SCRIPT_DIR/run.sh"

# Crée le répertoire de données si absent
mkdir -p "$SCRIPT_DIR/data"

# Assure que run.sh est exécutable
chmod +x "$RUN_SCRIPT"

# Construit les lignes cron (8h et 18h tous les jours)
CRON_8H="0 8 * * * $RUN_SCRIPT all"
CRON_18H="0 18 * * * $RUN_SCRIPT all"

# Ajoute les entrées cron sans dupliquer
(crontab -l 2>/dev/null | grep -v "find_job.*\(scraper.py\|run.sh\)"; echo "$CRON_8H"; echo "$CRON_18H") | crontab -

echo "Cron configuré :"
crontab -l | grep 'find_job.*run.sh'
echo ""
echo "Le scraper tournera tous les jours à 8h00 et 18h00."
echo "Les résultats sont dans : $SCRIPT_DIR/docs/index.html"
echo "Les logs sont dans      : $SCRIPT_DIR/data/scraper.log"
