#!/usr/bin/env python3
"""Agent de recherche d'emploi - Lettres Modernes - Genève"""

import json
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
LOG_FILE = DATA_DIR / "scraper.log"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

KEYWORDS = [
    "professeur français",
    "professeur de français",
    "enseignant français",
    "rédacteur",
    "rédactrice",
    "libraire",
    "éditeur",
    "éditrice",
    "correcteur",
    "correctrice",
    "journaliste",
    "lettres",
    "bibliothécaire",
    "chargé de communication",
    "chargée de communication",
    "attaché de presse",
    "attachée de presse",
    "maison d'édition",
    "traducteur",
    "traductrice",
]

EXCLUDE_KEYWORDS = [
    "informatique", "ingénieur", "développeur", "comptable",
    "médecin", "infirmier", "avocat", "électricien", "chauffeur",
    "technicien", "mécanicien",
]

SEARCH_TERMS = [
    "professeur français",
    "rédacteur",
    "libraire",
    "éditeur",
    "bibliothécaire",
    "journaliste",
    "correcteur",
    "traducteur",
    "communication",
]


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_seen() -> set:
    if SEEN_FILE.exists():
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False, indent=2)


def job_id(title: str, url: str) -> str:
    raw = f"{title.lower().strip()}{url.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


def is_relevant(title: str, description: str = "") -> bool:
    text = (title + " " + description).lower()
    if any(k.lower() in text for k in EXCLUDE_KEYWORDS):
        return False
    return any(k.lower() in text for k in KEYWORDS)


def fetch(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log(f"Erreur fetch {url} (tentative {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def extract_next_data(soup) -> dict:
    """Extrait le JSON embarqué dans les pages Next.js (__NEXT_DATA__)."""
    tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_jobup() -> list:
    """JobUp.ch — SPA Next.js avec JSON embarqué."""
    jobs = []
    for term in SEARCH_TERMS:
        url = (
            f"https://www.jobup.ch/fr/emplois/"
            f"?term={requests.utils.quote(term)}&location=Gen%C3%A8ve&publication-date=7"
        )
        soup = fetch(url)
        if not soup:
            continue

        data = extract_next_data(soup)
        try:
            # La structure varie ; on cherche la liste de vacances
            vacancies = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("searchResult", {})
                    .get("vacancies", [])
            )
        except (AttributeError, KeyError):
            vacancies = []

        for v in vacancies:
            title = v.get("title", "")
            company = v.get("company", {}).get("name", "—") if isinstance(v.get("company"), dict) else "—"
            slug = v.get("slug") or v.get("id", "")
            href = f"https://www.jobup.ch/fr/emplois/detail/{slug}/" if slug else ""
            if title and href and is_relevant(title):
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": href,
                    "source": "JobUp.ch",
                    "found_at": datetime.now().isoformat(),
                })
        time.sleep(1.5)

    log(f"JobUp.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_jobs_ch() -> list:
    """Jobs.ch — SPA Next.js avec JSON embarqué."""
    jobs = []
    for term in SEARCH_TERMS:
        url = (
            f"https://www.jobs.ch/fr/jobs/"
            f"?term={requests.utils.quote(term)}&location=Gen%C3%A8ve"
        )
        soup = fetch(url)
        if not soup:
            continue

        data = extract_next_data(soup)
        try:
            results = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("jobs", [])
            )
            if not results:
                results = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("searchResults", {})
                        .get("items", [])
                )
        except (AttributeError, KeyError):
            results = []

        for item in results:
            title = item.get("title", "") or item.get("name", "")
            company = item.get("company", {}).get("name", "—") if isinstance(item.get("company"), dict) else "—"
            slug = item.get("slug") or item.get("id", "")
            href = f"https://www.jobs.ch/fr/jobs/{slug}/" if slug else ""
            if title and href and is_relevant(title):
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": href,
                    "source": "Jobs.ch",
                    "found_at": datetime.now().isoformat(),
                })
        time.sleep(1.5)

    log(f"Jobs.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_jobscout24() -> list:
    """JobScout24.ch — HTML traditionnel."""
    jobs = []
    for term in SEARCH_TERMS:
        url = (
            f"https://www.jobscout24.ch/fr/jobs"
            f"?query={requests.utils.quote(term)}&location=Gen%C3%A8ve"
        )
        soup = fetch(url)
        if not soup:
            continue

        for li in soup.select("li.job-list-item"):
            title_el = li.select_one("a.job-title, a.job-link-detail")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            detail_url = li.get("data-job-detail-url", "")
            if not detail_url:
                a = li.select_one("a[href]")
                detail_url = a["href"] if a else ""
            if detail_url and not detail_url.startswith("http"):
                detail_url = "https://www.jobscout24.ch" + detail_url
            # company is in first <span> inside p.job-attributes
            spans = li.select("p.job-attributes span")
            company = spans[0].get_text(strip=True) if spans else "—"
            if title and detail_url and is_relevant(title):
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": detail_url,
                    "source": "JobScout24.ch",
                    "found_at": datetime.now().isoformat(),
                })
        time.sleep(1.5)

    log(f"JobScout24.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_letemps() -> list:
    """Le Temps Emploi — page de listing (pas de filtre par mot-clé en URL)."""
    jobs = []
    url = "https://www.letemps.ch/emploi"
    soup = fetch(url)
    if not soup:
        return jobs

    for card in soup.select("li.job.card"):
        title_el = card.select_one("h3.job-title > a.stretched-link, a.stretched-link")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.letemps.ch" + href
        company_el = card.select_one(".job-provider")
        company = company_el.get_text(strip=True) if company_el else "—"
        if title and href and is_relevant(title):
            jobs.append({
                "title": title,
                "company": company,
                "url": href,
                "source": "Le Temps Emploi",
                "found_at": datetime.now().isoformat(),
            })

    log(f"Le Temps Emploi: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_ge_ch() -> list:
    """Offres de l'État de Genève."""
    jobs = []
    url = "https://www.ge.ch/offres-emploi-etat-geneve/liste-offres"
    soup = fetch(url)
    if not soup:
        return jobs

    for article in soup.select("article"):
        title_el = (
            article.select_one("div.text-title-medium a")
            or article.select_one("a[rel='bookmark']")
            or article.select_one("a")
        )
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.ge.ch" + href
        if title and href and is_relevant(title):
            jobs.append({
                "title": title,
                "company": "État de Genève",
                "url": href,
                "source": "ge.ch",
                "found_at": datetime.now().isoformat(),
            })

    log(f"ge.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Rapport HTML
# ---------------------------------------------------------------------------

def generate_html(new_jobs: list, all_jobs: list):
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")

    def rows(job_list, css_class=""):
        out = ""
        for j in job_list:
            out += (
                f'<tr class="{css_class}">'
                f'<td><a href="{j["url"]}" target="_blank">{j["title"]}</a></td>'
                f'<td>{j["company"]}</td>'
                f'<td>{j["source"]}</td>'
                f'<td>{j["found_at"][:16].replace("T", " ")}</td>'
                f'</tr>\n'
            )
        return out

    section_new = (
        "<p>Aucune nouvelle offre depuis la dernière recherche.</p>"
        if not new_jobs
        else (
            f'<table><thead><tr><th>Poste</th><th>Entreprise</th>'
            f'<th>Source</th><th>Trouvé le</th></tr></thead>'
            f'<tbody>{rows(new_jobs, "new")}</tbody></table>'
        )
    )

    sorted_all = sorted(all_jobs, key=lambda x: x["found_at"], reverse=True)
    section_all = (
        "<p>Aucune offre trouvée.</p>"
        if not all_jobs
        else (
            f'<table><thead><tr><th>Poste</th><th>Entreprise</th>'
            f'<th>Source</th><th>Trouvé le</th></tr></thead>'
            f'<tbody>{rows(sorted_all)}</tbody></table>'
        )
    )

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Offres d'emploi – Lettres Modernes – Genève</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1100px; margin: 2rem auto; color: #222; }}
  h1 {{ color: #1a56db; }}
  h2 {{ margin-top: 2rem; border-bottom: 2px solid #e5e7eb; padding-bottom: .5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th {{ background: #1a56db; color: white; padding: .6rem 1rem; text-align: left; }}
  td {{ padding: .5rem 1rem; border-bottom: 1px solid #e5e7eb; }}
  tr.new td {{ background: #fefce8; }}
  tr:hover td {{ background: #f0f9ff; }}
  a {{ color: #1a56db; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .badge {{ background: #16a34a; color: white; border-radius: 4px;
             padding: 2px 8px; font-size: .8rem; margin-left: .5rem; }}
  .updated {{ color: #6b7280; font-size: .9rem; }}
</style>
</head>
<body>
<h1>Offres d'emploi – Lettres Modernes – Genève</h1>
<p class="updated">Dernière mise à jour : {now}</p>

<h2>Nouvelles offres <span class="badge">{len(new_jobs)}</span></h2>
{section_new}

<h2>Toutes les offres ({len(all_jobs)})</h2>
{section_all}
</body>
</html>"""

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Rapport HTML mis à jour : {RESULTS_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_all_jobs() -> list:
    p = DATA_DIR / "all_jobs.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_all_jobs(jobs: list):
    p = DATA_DIR / "all_jobs.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def main():
    log("=== Démarrage de la recherche d'emploi ===")
    seen = load_seen()
    all_jobs = load_all_jobs()

    raw = []
    # JobUp.ch et Jobs.ch sont protégés par AWS WAF (captcha) — ignorés
    raw.extend(scrape_jobscout24())
    raw.extend(scrape_letemps())
    raw.extend(scrape_ge_ch())

    new_jobs = []
    for job in raw:
        jid = job_id(job["title"], job["url"])
        if jid not in seen:
            seen.add(jid)
            new_jobs.append(job)
            all_jobs.append(job)

    log(f"Nouvelles offres : {len(new_jobs)} | Total cumulé : {len(all_jobs)}")
    save_seen(seen)
    save_all_jobs(all_jobs)
    generate_html(new_jobs, all_jobs)
    log("=== Recherche terminée ===\n")


if __name__ == "__main__":
    main()
