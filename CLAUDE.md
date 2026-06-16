# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Daily job-search scraper for a *Lettres Modernes* graduate, targeting the Geneva
area + the nearby Nyon district (Vaud). It scrapes a dozen Swiss job sources,
filters by keyword relevance + geography + language (French), then publishes an
HTML report to GitHub Pages (`https://gabigbarig.github.io/find_job/`) and emails
new matches. Everything lives in a single file: `scraper.py`. The UI and docs are
in French — match that when editing user-facing strings, logs, and comments.

## Commands

```bash
./run.sh        # run the scraper, append output to data/scraper.log (used by cron)
./view.sh       # run, then open data/results.html in the Windows browser (WSL)
./publish.sh    # run, then git add docs/index.html, commit, and push (publishes to Pages)
./setup_cron.sh # install cron jobs at 08:00 and 18:00 daily
python3 scraper.py   # run directly (uses ./venv if invoked via the *.sh wrappers)
```

There are no tests and no linter. To exercise a single source in isolation, call
its scraper function directly, e.g.:

```bash
./venv/bin/python3 -c "import scraper; print(scraper.scrape_unige())"
```

Dependencies (installed in `./venv`, no `requirements.txt`): `requests`,
`beautifulsoup4`, `lxml`, and optionally `playwright` (needs a system Chromium —
see Playwright note below).

## Configuration

Secrets load from `.env` (gitignored) via a minimal built-in parser — no
`python-dotenv`. Relevant vars (all optional; absent ones disable their feature):

- `ADZUNA_ID` / `ADZUNA_KEY` — Adzuna API; without them `scrape_adzuna` is skipped.
- `SMTP_FROM` / `SMTP_PASS` / `SMTP_TO` — Gmail SSL alert email; without them no email is sent.
- `RESPECT_ROBOTS` (default on), `EXPIRY_DAYS` (60), `POLITE_DELAY` (1.0s),
  `FETCH_DESCRIPTIONS` (on), `MAX_DETAIL_FETCHES` (40).

`data/` and `docs/` are created at runtime. `data/` is gitignored; **only
`docs/index.html` (and `docs/feed.xml`) are committed** — that is what GitHub
Pages serves.

## Architecture

One pipeline, orchestrated by `main()`:

1. Load persisted state from `data/`: `seen_jobs.json` (dedup hashes),
   `all_jobs.json` (full archive), `health.json` (per-source history).
2. Expire jobs older than `EXPIRY_DAYS` and drop non-French archived titles.
3. Run every function in the `SCRAPERS` list, each isolated in a try/except so one
   failure never stops the others.
4. Dedup new jobs (by `job_id` = md5 of title+url, **and** by title+company).
5. Persist state, regenerate `docs/index.html` + `docs/feed.xml`, send email.

### Adding or fixing a source

Each `scrape_*()` returns a list of job dicts and is registered in the `SCRAPERS`
list near the bottom. The shared helper **`consider(title, url, base_fields, jobs,
seen_urls)`** is the funnel almost every scraper feeds into — it applies, in order:
French-language check → relevance (title alone, or title+description if the title
is "ambiguous") → geographic zone filter → enrichment (`finalize`: relevance score
+ activity-rate `taux`) → append. A few sources (`scrape_vaud`, `scrape_adzuna`,
`scrape_indeed_pw`) bypass `consider` and inline the same checks because they get
descriptions from an API/JS and build the job dict themselves.

When writing a scraper, isolate the CSS selector at the top of the function (the
existing ones are commented "SÉLECTEUR À AJUSTER SI BESOIN") — these are the
fragile parts. Use `fetch(url)` for plain HTML (handles robots.txt, polite
per-domain delay, retry with back-off, and fail-fast on DNS/403/404).

### Relevance & filtering model

The matching logic is keyword-list driven and accent-insensitive (`normalize()`):

- `KEYWORDS` — positive terms; `EXCLUDE_KEYWORDS` — hard rejects (incl. FLE, which
  is deliberately *out* of scope). `is_relevant()` rejects on any exclude, accepts
  on any keyword, or accepts a teaching-term + Lettres-subject combination.
- Geography: `GENEVE_ZONE` + nearby `VAUD_ZONE` are accepted; `GEO_FAR` (Lausanne,
  Zürich, etc.) is rejected. `in_zone()` is **tolerant**: no location signal → kept.
- Language: `is_french_text()` rejects clearly-German titles. Also tolerant by design.
- Scoring: `relevance_score()` weights title matches double; the HTML report and
  email sort by this score.

The guiding principle throughout is "better to surface one extra offer than miss a
mislabeled one" — filters lean toward keeping when uncertain.

### Source health auto-diagnostic

`update_health()` tracks each source's count history in `data/health.json` and
raises an alert when a source that used to return results drops to 0 (likely a
broken selector) or has never returned anything over many runs. Alerts go into the
log and the email. When a source breaks, prefer fixing its selector over removing
it from `SCRAPERS`.

### Playwright / anti-bot

Sources behind a JS anti-bot wall (myScience, Indeed) use Playwright with a
stealth Chromium context (`_new_stealth_context`, `_STEALTH_INIT_JS`). This needs
a **system Chromium** (`sudo apt install chromium-browser`); if none is found,
`PLAYWRIGHT_AVAILABLE` is False and those sources are skipped gracefully.
