# Procédure d'installation et d'utilisation de l'agent-bus

Système de coordination multi-IA (Claude, Codex, etc.) via un broker Redis partagé.
Permet de lancer plusieurs agents en parallèle qui se synchronisent (état, directives,
validation 4-yeux) et un tableau de bord `busmon`.

Déjà préparé (rien à refaire) :
- Dépôt cloné : ~/git_projects/agent-bus-monitor
- Config broker : ~/git_projects/agent-bus-monitor/.env (Redis localhost:6380)
- Script d'install : ~/git_projects/agent-bus-monitor/setup-bus.sh
- Helper projet : ~/git_projects/find_job/agent-bus.env (namespace AGENT_BUS_PROJECT=find_job)

Prérequis machine : WSL2 Ubuntu avec systemd actif. Go n'est PAS nécessaire (les
binaires se compilent dans un conteneur Docker).

===============================================================================
ÉTAPE 1 — Installer Docker
  Où : terminal WSL (ou dans le chat Claude avec le préfixe !). Demande le sudo.
-------------------------------------------------------------------------------
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sudo sh /tmp/get-docker.sh
sudo usermod -aG docker $USER && sudo systemctl enable --now docker

===============================================================================
ÉTAPE 2 — Redémarrer WSL (active l'appartenance au groupe docker)
  Où : PowerShell ou CMD WINDOWS (pas WSL).
-------------------------------------------------------------------------------
wsl --shutdown
# puis rouvrir le terminal WSL / Claude Code

===============================================================================
ÉTAPE 3 — Vérifier Docker (doit marcher SANS sudo)
  Où : terminal WSL.
-------------------------------------------------------------------------------
docker run --rm hello-world
# Doit afficher "Hello from Docker!"

===============================================================================
ÉTAPE 4 — Installer l'agent-bus (Redis + binaires agentbus/busmon)
  Où : terminal WSL.
-------------------------------------------------------------------------------
bash ~/git_projects/agent-bus-monitor/setup-bus.sh

===============================================================================
ÉTAPE 5 — Mettre ~/bin dans le PATH (une seule fois)
  Où : terminal WSL.
-------------------------------------------------------------------------------
grep -q 'HOME/bin' ~/.bashrc || echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
which agentbus busmon

===============================================================================
ÉTAPE 6 — Utiliser le bus depuis find_job
  Où : terminal WSL, dans ~/git_projects/find_job.
-------------------------------------------------------------------------------
cd ~/git_projects/find_job
source agent-bus.env                 # charge AGENT_BUS_PROJECT=find_job + Redis
agentbus notify "bus opérationnel sur find_job"
agentbus status claude1 working "je bosse sur le scraper"

# Tableau de bord, à laisser tourner dans SON PROPRE terminal :
busmon --project find_job

===============================================================================
ÉTAPE 7 — Lancer 2 agents en parallèle (un terminal PAR agent)
  Où : terminaux WSL, chacun dans ~/git_projects/find_job.
-------------------------------------------------------------------------------
# Terminal A (Claude) :
cd ~/git_projects/find_job
AGENT_BUS_AGENT=claude1 source agent-bus.env
agentbus status claude1 working "implémente la source X"
agentbus subscribe claude1           # écoute les directives qui lui sont adressées

# Terminal B (autre IA, ex. Codex) :
cd ~/git_projects/find_job
AGENT_BUS_AGENT=codex1 source agent-bus.env
agentbus status codex1 idle
agentbus subscribe codex1

# Coordination (depuis n'importe quel terminal déjà "sourcé") :
agentbus cmd codex1 "relis scraper.py et challenge mon dédup"   # directive
agentbus challenge claude1 "ton parsing FLE rate les titres EN"  # validation 4-yeux
agentbus verdict --ref <REF> claude1 approve                     # <REF> vu dans busmon/listen
agentbus report claude1 "source bibliosuisse livrée"             # jalon pour l'humain

===============================================================================
AIDE-MÉMOIRE DES COMMANDES
-------------------------------------------------------------------------------
agentbus status <agent> <working|idle|blocked|done> [message]
agentbus report <agent> [message]
agentbus notify [message]
agentbus cmd <cible> [commande]
agentbus challenge <cible> [pourquoi]
agentbus verdict --ref <REF> <cible> approve|reject
agentbus subscribe <agent> [idle_secs]
agentbus listen [status report notify cmd]
busmon --project <projet>

===============================================================================
PIÈGES COURANTS
-------------------------------------------------------------------------------
- --project obligatoire SAUF si AGENT_BUS_PROJECT est exporté (fait par agent-bus.env).
- Toujours "--project find_job" (double tiret + espace), jamais "=".
- Ordre fixe : <agent> puis <état>, message en dernier.
- Noms d'agents : minuscules, commencent par une lettre, [a-z0-9_-], max 32 car.
- Le broker écoute sur 127.0.0.1:6380 uniquement (mot de passe en clair → jamais exposé au LAN).

===============================================================================
DÉPANNAGE
-------------------------------------------------------------------------------
- "permission denied" sur docker.sock : groupe docker pas actif → refaire ÉTAPE 2.
- "agentbus: command not found" : ~/bin pas dans le PATH → refaire ÉTAPE 5.
- Vérifier que Redis tourne :   docker compose -f ~/git_projects/agent-bus-monitor/docker-compose.yml ps
- Redémarrer le broker :        docker compose -f ~/git_projects/agent-bus-monitor/docker-compose.yml restart
- Logs du broker :              docker logs agent-bus-redis
