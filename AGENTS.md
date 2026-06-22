# AGENTS.md

Guidance for Codex and other coding agents working in this repository.

## Project

Daily job-search scraper for a *Lettres Modernes* graduate, targeting the Geneva
area + nearby Nyon district (Vaud). It scrapes Swiss job sources, filters by
keyword relevance + geography + French language, then publishes an HTML report to
GitHub Pages and an RSS feed. Most behavior lives in `scraper.py`.

User-facing UI, logs, docs, and messages are in French. Match that when editing
visible text.

## Commands

```bash
./run.sh        # run scraper, append output to data/scraper.log
./view.sh       # run, then open data/results.html in the Windows browser (WSL)
./publish.sh    # run, then commit/push docs/index.html + docs/feed.xml
./setup_cron.sh # install cron jobs at 08:00 and 18:00 daily
python3 scraper.py
```

There are no formal tests or linter. For a focused check, call one scraper
directly:

```bash
./venv/bin/python3 -c "import scraper; print(scraper.scrape_unige())"
```

Dependencies are installed in `./venv`: `requests`, `beautifulsoup4`, `lxml`, and
optionally `playwright` with a system Chromium.

## Working Rules

- Keep changes narrow. This project is intentionally mostly single-file.
- Do not touch `.env`, `data/`, or generated `docs/*` unless the task explicitly
  involves publishing or output generation.
- `docs/index.html` and `docs/feed.xml` are generated publish artifacts.
- If the worktree is already dirty, preserve unrelated user changes.
- Prefer fixing a broken selector/source over removing it from `SCRAPERS`.
- When adding a source, put fragile CSS selectors near the top of the scraper
  function and register the function in `SCRAPERS`.

## Filtering Model

Most scrapers should feed candidates through:

```python
consider(title, url, base_fields, jobs, seen_urls)
```

That helper applies French-language filtering, relevance, geography, scoring, and
append logic. Some API/JS-backed scrapers inline similar checks because they build
richer job dicts directly.

Filtering is deliberately tolerant: when location or language signals are
uncertain, prefer surfacing a plausible offer over dropping it silently.

## Two-Agent Codex Workflow

Use two Codex sessions only for work that benefits from review or parallel
diagnosis. Default roles:

- `codex1`: implementation owner.
- `codex2`: reviewer/diagnostic agent.

Recommended terminal layout:

```bash
# Terminal 1: dashboard
cd ~/git_projects/find_job
source agent-bus.env
busmon --project find_job

# Terminal 2: implementation agent
cd ~/git_projects/find_job
export AGENT_BUS_AGENT=codex1
source agent-bus.env
codex --cd ~/git_projects/find_job --model gpt-5.5 --config model_reasoning_effort='"xhigh"' --config sandbox_workspace_write.network_access=true --sandbox workspace-write --ask-for-approval never

# Terminal 3: reviewer agent
cd ~/git_projects/find_job
export AGENT_BUS_AGENT=codex2
source agent-bus.env
codex --cd ~/git_projects/find_job --model gpt-5.5 --config model_reasoning_effort='"xhigh"' --config sandbox_workspace_write.network_access=true --sandbox workspace-write --ask-for-approval never
```

This auto-approves actions inside the repository workspace and allows access to
the local agent-bus Redis broker. Do not use
`--dangerously-bypass-approvals-and-sandbox` for normal project work.

In each Codex session, ask the agent to announce itself and arm bus listening:

```text
Annonce-toi sur agent-bus avec ton identité, puis arme `agentbus subscribe <toi>`
en tâche d'arrière-plan. Ré-arme l'écoute après chaque directive reçue.
```

Use the bus for explicit review hand-offs:

```bash
agentbus cmd codex2 relis le diff de codex1 : cherche regressions, filtrage cassé, selecteurs fragiles, oublis dans SCRAPERS, et propose uniquement les corrections nécessaires
```

Before committing or publishing, use the 4-eyes gate when the change is risky:

```bash
agentbus challenge codex2 valide le diff avant publication
agentbus reply --ref <REF> codex2 <avis>
agentbus verdict --ref <REF> codex2 approve <raison>
```

Do not run two agents that both edit the same file freely. One agent owns the
implementation; the other reviews and comments unless explicitly asked to patch a
specific issue.
