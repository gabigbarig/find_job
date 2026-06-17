# Démarrage de la collaboration multi-agents sur `find_job`

Comment faire travailler **deux agents Claude ensemble** (et, plus tard, Codex) via
l'agent-bus, sur ce dépôt. Pour l'**installation** des binaires/broker, voir
`agent-bus-INSTALL.md`. Ce fichier-ci décrit le **lancement au quotidien**.

---

## En une phrase

Le bus (Redis + `agentbus`/`busmon`) est déjà installé et les hooks Claude Code de
ce projet sont déjà posés. Pour collaborer, il suffit d'**ouvrir trois terminaux**
(un dashboard + deux agents) ; les agents s'annoncent tout seuls sur le bus.

---

## Ce qui est déjà en place (rien à refaire)

- **Broker Redis** sur `127.0.0.1:6380` (conteneur `agent-bus-redis`).
- **Binaires** `agentbus` et `busmon` dans `~/bin` (dans le `PATH`).
- **Hooks Claude Code** dans `.claude/settings.local.json` (gitignoré) :
  - `SessionStart` → l'agent se déclare `status working` sur le bus ;
  - `Stop` → l'agent publie `report --auto` (filet de sécurité) puis `status idle`.
  - L'identité vient de `agent-bus.env` (défaut `claude1`). Hooks **non-bloquants** :
    si Redis est éteint, la session Claude n'est jamais gênée.
- Helper `agent-bus.env` : `source agent-bus.env` exporte `AGENT_BUS_PROJECT=find_job`,
  le mot de passe Redis et met `~/bin` dans le `PATH`.

---

## Lancement — trois terminaux, dans `~/git_projects/find_job`

### Terminal 1 — tableau de bord (à laisser tourner)
```bash
cd ~/git_projects/find_job
source agent-bus.env
busmon --project find_job
```

### Terminal 2 — Claude n°1
```bash
cd ~/git_projects/find_job
AGENT_BUS_AGENT=claude1 claude --model claude-opus-4-8 --effort max
```

### Terminal 3 — Claude n°2
```bash
cd ~/git_projects/find_job
AGENT_BUS_AGENT=claude2 claude --model claude-opus-4-8 --effort max
```

> **Modèle & « mode max »** : `--model claude-opus-4-8` = Opus 4.8, `--effort max` =
> effort maximum (niveaux : low/medium/high/xhigh/max). Le modèle est aussi fixé en
> dur dans `.claude/settings.local.json` (`"model": "claude-opus-4-8"`), donc même un
> `claude` sans flag utilise Opus 4.8 ici ; le `--effort max`, lui, se passe au lancement
> (ou via `/effort max` une fois la session ouverte).

Grâce aux hooks, **les deux sessions apparaissent automatiquement** dans `busmon`
en `working` dès l'ouverture. Rien d'autre à taper pour qu'elles soient « vivantes ».

---

## Armer l'écoute des directives (dans chaque session Claude)

La réception d'une directive (`cmd`) passe par `agentbus subscribe`, **armé comme une
tâche d'arrière-plan** par la session : il bloque sur le flux `:cmd`, imprime la
commande reçue, puis **sort** — et cette sortie réveille la session, qui se ré-arme.
C'est le modèle « wake-on-exit » du projet. **Ne pas** l'envelopper dans une boucle
`while` ni dans un démon (ça ne réveille jamais une session terminal).

Le plus simple : le demander à l'agent en langage naturel, dans chaque session :

> « arme l'écoute du bus : `agentbus subscribe <ton identité>` en tâche d'arrière-plan,
>  et ré-arme à chaque réveil. »

---

## Faire collaborer les agents

Depuis n'importe quel terminal déjà `source`-é (ou en le demandant à un agent) :

```bash
# Directive : claude1 demande quelque chose à claude2
agentbus cmd claude2 relis scraper.py et challenge mon dedup

# Gate « 4-yeux » : bloque la cible jusqu'à un verdict (CAPTURE le ref affiché)
agentbus challenge claude2 confirme que jobs.ch ramene 0 a cause du selecteur
#   → challenge <REF> opened on claude2

# La cible voit qu'elle est gatée, et répond
agentbus gate claude2                       # exit != 0 = bloqué ; liste les challenges
agentbus reply --ref <REF> claude2 oui, vieille classe CSS ciblee

# Un second relecteur ferme le gate
agentbus verdict --ref <REF> claude2 approve analyse correcte

# Rapport (jalon lisible par l'humain dans busmon)
agentbus report claude2 selecteur jobs.ch identifie
```

### Aide-mémoire des commandes
```
agentbus status  <agent> <working|idle|blocked|done> [message]
agentbus report  <agent> [--auto] [message]
agentbus notify  [message]
agentbus cmd     <cible> [commande]
agentbus challenge <cible> [--ref R] [pourquoi]
agentbus reply   --ref <R> <cible> [reponse]
agentbus verdict --ref <R> <cible> approve|reject [message]
agentbus gate    <agent>            # exit != 0 si gaté
agentbus subscribe <agent> [idle_secs]
agentbus listen  [status report notify cmd]
busmon --project find_job
```

**Pièges** (qui font échouer une commande pourtant bien écrite) :
- `--project` obligatoire, **sauf** si `AGENT_BUS_PROJECT` est exporté (fait par `agent-bus.env`).
- Flags en **double tiret + espace** : `--ref abc`, jamais `--ref=abc` ni `-ref`.
- Ordre fixe : `<agent>` puis l'`<état>`/`<cible>`, le message en dernier.
- Noms d'agents : `^[a-z][a-z0-9_-]{0,31}$` (minuscule, commence par une lettre).

---

## Ce qui n'est PAS disponible sur cette machine (et pourquoi)

Ce ne sont pas des oublis, mais des dépendances absentes :

1. **Codex en agent vivant** — aucun CLI `codex` n'est installé ici. Pour l'instant
   on collabore avec **deux Claude** (`claude1` + `claude2`). Pour ajouter un vrai
   Codex : installer son CLI puis le câbler sur le bus (`AGENT_BUS_AGENT=codex1`).
2. **`hermes` / Signal** — `hermes` n'est **pas** le lien entre les agents : c'est un
   relais *optionnel* qui pousse des rapports filtrés vers Signal depuis une machine
   **distante** (gateway `:8644`). Cette infra n'existe pas ici, et elle n'est pas
   nécessaire pour que les agents collaborent : ils le font directement sur le bus.

---

## Template : demander l'ajout d'une source (à deux)

Prérequis : les deux agents écoutent (badge `👂` dans `busmon`). Ensuite, **collez ceci
dans le Terminal de claude1** en remplaçant les `<…>` :

> Nouvelle tâche commune avec claude2 sur le bus find_job : **ajouter la source `<NOM>`**
> (`<URL_DE_LA_LISTE_D_OFFRES>`).
>
> Rappels projet (cf. `CLAUDE.md`) : une source est une fonction `scrape_<nom>()` qui
> renvoie une liste de dicts d'offres, **isole son sélecteur CSS en tête de fonction**,
> utilise `fetch(url)` pour le HTML, fait passer chaque offre par le helper
> `consider(title, url, base_fields, jobs, seen_urls)` (filtre langue → pertinence →
> zone → enrichissement), et est enregistrée dans la liste `SCRAPERS`. On ajuste un
> sélecteur, on ne contourne pas les filtres.
>
> Répartition :
> - **Toi (claude1)** : écris `scrape_<nom>()` en t'inspirant d'une source proche
>   existante (ex. `scrape_unige`), enregistre-la dans `SCRAPERS`, et teste-la en
>   isolement : `./venv/bin/python3 -c "import scraper; print(scraper.scrape_<nom>())"`.
> - **Délègue la revue à claude2** :
>   `agentbus cmd claude2 relis scrape_<nom> : verifie filtrage zone/langue, robustesse du selecteur CSS, gestion d'erreur de fetch(), et enregistrement dans SCRAPERS ; challenge si fragile`.
> - Émettez `status`/`report` au fil de l'eau. **Revue croisée 4-yeux**
>   (`challenge`/`verdict`) avant tout commit, et **ne committez qu'après validation
>   des deux côtés**.

### Exemple rempli (Université de Lausanne)

> Nouvelle tâche commune avec claude2 : **ajouter la source `unil`**
> (`https://www.unil.ch/central/home/menuinst/travailler-a-lunil.html`).
> Rappels projet : `scrape_unil()` renvoie une liste de dicts, sélecteur CSS isolé en
> tête, `fetch(url)` pour le HTML, chaque offre passe par `consider(...)`, fonction
> enregistrée dans `SCRAPERS`.
> - Toi (claude1) : écris `scrape_unil()` sur le modèle de `scrape_unige`, enregistre-la
>   dans `SCRAPERS`, teste avec `./venv/bin/python3 -c "import scraper; print(scraper.scrape_unil())"`.
> - Délègue : `agentbus cmd claude2 relis scrape_unil : filtrage zone/langue + selecteur + fetch + SCRAPERS ; challenge si fragile`.
> - Revue croisée 4-yeux avant commit ; ne committez qu'après validation des deux.

> Pour une autre nature de tâche (réparer une source, ajouter un mot-clé, etc.),
> reprenez la même structure : **objectif → qui fait quoi → revue croisée avant commit**.

## Dépannage rapide

- `agentbus: command not found` → `~/bin` pas dans le `PATH` : `source agent-bus.env`.
- Un agent n'apparaît pas dans `busmon` → vérifier que le broker tourne :
  `docker compose -f ~/git_projects/agent-bus-monitor/docker-compose.yml ps`.
- Redémarrer le broker :
  `docker compose -f ~/git_projects/agent-bus-monitor/docker-compose.yml restart`.
- Voir le trafic brut sans le TUI : `agentbus listen`.
