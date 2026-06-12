#!/usr/bin/env python3
"""Agent de recherche d'emploi - Lettres Modernes - Genève"""

import json
import re
import shutil
import time
import hashlib
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    # Playwright est installé mais ses binaires manquent sur Ubuntu 26.04 ;
    # on cherche un Chromium système (snap ou apt) pour le remplacer.
    _CHROMIUM_PATH = (
        shutil.which("chromium-browser")
        or shutil.which("chromium")
        or ("/snap/bin/chromium" if Path("/snap/bin/chromium").exists() else None)
    )
    PLAYWRIGHT_AVAILABLE = _CHROMIUM_PATH is not None
    if not PLAYWRIGHT_AVAILABLE:
        print(
            "[INFO] Playwright installé mais aucun Chromium système trouvé.\n"
            "       Pour activer Indeed : sudo snap install chromium"
        )
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    _CHROMIUM_PATH = None

# --- Adzuna API (agrège Indeed, LinkedIn, et +200 boards) ---
# Inscription gratuite sur https://developer.adzuna.com/
ADZUNA_ID  = "e91c32be"
ADZUNA_KEY = "e649b514710a4094554f36782966fb30"

# --- Alertes email (optionnel) ---
# Remplir pour recevoir un email à chaque nouvelle offre trouvée.
# SMTP_PASS = mot de passe d'application Google (16 caractères), PAS ton vrai mot de passe.
# Créer sur https://myaccount.google.com/apppasswords
SMTP_FROM = ""   # ex: "tonprenom@gmail.com"
SMTP_PASS = ""

# Offres de plus de EXPIRY_DAYS jours retirées de la base à chaque run
EXPIRY_DAYS = 60

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
PUBLIC_FILE = DOCS_DIR / "index.html"
LOG_FILE = DATA_DIR / "scraper.log"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

# Gardé pour les scrapers qui construisent leurs propres headers Oracle/Adzuna
HEADERS = dict(SESSION.headers)

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
    "archiviste",
    "documentaliste",
    "médiateur culturel",
    "médiatrice culturelle",
    "chargé de projet culturel",
    "chargée de projet culturel",
    "animateur culturel",
    "animatrice culturelle",
    "muséologue",
    "responsable culturel",
    "responsable de collection",
    "chargé des publics",
    "chargée des publics",
    "médiation culturelle",
    # Terminologie suisse (ge.ch, école de commerce, etc.)
    "maître d'enseignement général / français",
    "maître d'enseignement général - français",
    "maître d'enseignement général – français",
    "maîtresse d'enseignement général / français",
    "maîtresse d'enseignement général - français",
    "maîtresse d'enseignement général – français",
    "français langue étrangère",
    "français cdd",
    "expression orale",
    "diction",
    "culture générale",
]

# Termes désignant un enseignant (ge.ch, jura.ch, etc. utilisent des conventions différentes)
TEACHING_TERMS = [
    "enseignement", "enseignant", "enseignante",
    "maître", "maîtresse", "professeur", "professeure",
]

# Matières liées aux Lettres Modernes
LETTRES_SUBJECTS = [
    "français", "lettres", "littérature", "expression orale",
    "diction", "français langue étrangère", "culture générale",
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
    "médiateur culturel",
    "archiviste",
    "musée",
    "médiation culturelle",
    "patrimoine",
]

# Communes et lieux du district de Nyon (Vaud) acceptables pour la zone Genève–Gland
VAUD_ZONE = {
    "nyon", "gland", "coppet", "prangins", "rolle",
    "mies", "tannay", "commugny", "founex", "bogis",
    "chavannes-de-bogis", "chavannes-des-bois", "borex",
    "eysins", "signy", "genolier", "gingins", "givrins",
    "arzier", "saint-cergue", "coinsins", "vich", "grens",
    "dully", "luins", "gilly", "tartegnin", "perroy",
    "allaman", "aubonne",
}

# Communes du canton de Genève
GENEVE_ZONE = {
    "genève", "geneva", "carouge", "lancy", "meyrin", "vernier", "onex",
    "plan-les-ouates", "thônex", "bernex", "chêne-bougeries", "chêne-bourg",
    "pregny-chambésy", "grand-saconnex", "satigny", "dardagny", "russin",
    "avully", "avusy", "cartigny", "chancy", "laconnex", "soral", "gy",
    "jussy", "choulex", "cologny", "vandoeuvres", "puplinge", "presinge",
    "meinier", "collonge-bellerive", "hermance", "anières", "corsier",
    "céligny", "bellevue", "genthod", "versoix", "collex-bossy",
}


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


def expire_old_jobs(all_jobs: list, seen: set) -> tuple:
    """Retire les offres de plus de EXPIRY_DAYS jours et leurs hashes de seen."""
    cutoff = datetime.now() - timedelta(days=EXPIRY_DAYS)
    fresh, expired_ids = [], set()
    for j in all_jobs:
        try:
            found_at = datetime.fromisoformat(j["found_at"])
        except (KeyError, ValueError):
            fresh.append(j)
            continue
        if found_at >= cutoff:
            fresh.append(j)
        else:
            expired_ids.add(job_id(j["title"], j["url"]))
    removed = len(all_jobs) - len(fresh)
    if removed:
        log(f"Expiration : {removed} offre(s) de plus de {EXPIRY_DAYS} jours retirées")
    return fresh, seen - expired_ids


def is_relevant(title: str, description: str = "") -> bool:
    text = (title + " " + description).lower()
    if any(k.lower() in text for k in EXCLUDE_KEYWORDS):
        return False
    if any(k.lower() in text for k in KEYWORDS):
        return True
    # Détecte "Enseignant de français", "Maître d'enseignement / Français", etc.
    if any(t in text for t in TEACHING_TERMS) and any(s in text for s in LETTRES_SUBJECTS):
        return True
    return False


def fetch(url: str, retries: int = 3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log(f"Erreur fetch {url} (tentative {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_ville_geneve() -> list:
    """Offres de la Ville de Genève (administration municipale)."""
    jobs = []
    url = (
        "https://www.ville-geneve.ch/autorites-administration/"
        "administration-municipale/travailler-ville-geneve/offres-emploi/"
    )
    soup = fetch(url)
    if not soup:
        return jobs

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/administration-municipale/offres-emploi/" not in href:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        if not href.startswith("http"):
            href = "https://www.ville-geneve.ch" + href
        if is_relevant(title):
            jobs.append({
                "title": title,
                "company": "Ville de Genève",
                "url": href,
                "source": "ville-geneve.ch",
                "location": "Genève",
                "found_at": datetime.now().isoformat(),
            })

    seen_urls = set()
    unique = []
    for j in jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique.append(j)

    log(f"ville-geneve.ch: {len(unique)} offre(s) trouvée(s)")
    return unique


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
        loc_el = card.select_one(".job-location, .location, [data-location]")
        location = loc_el.get_text(strip=True) if loc_el else "Suisse romande"
        if title and href and is_relevant(title):
            jobs.append({
                "title": title,
                "company": company,
                "url": href,
                "source": "Le Temps Emploi",
                "location": location,
                "found_at": datetime.now().isoformat(),
            })

    log(f"Le Temps Emploi: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_vaud() -> list:
    """Offres de l'État de Vaud via Oracle HCM REST API."""
    jobs = []
    oracle_base = "https://fa-ewrg-saasfaeuraprod1.fa.ocs.oraclecloud.com"
    api_url = (
        f"{oracle_base}/hcmRestApi/resources/11.13.18.05/"
        "recruitingCEJobRequisitions"
        "?expand=requisitionList"
        "&finder=findReqs;siteNumber=CX_2,limit=500"
    )
    headers_oracle = {
        **HEADERS,
        "Accept": "application/json",
        "ora-irc-vanity-domain": "Y",
    }
    try:
        r = SESSION.get(api_url, headers=headers_oracle, timeout=20)
        r.raise_for_status()
        data = r.json()
        reqs = data.get("items", [{}])[0].get("requisitionList", [])
        found = 0
        for job in reqs:
            title = job.get("Title", "").strip()
            jid = job.get("Id")
            if not title or not jid:
                continue
            short_desc = job.get("ShortDescriptionStr") or ""
            loc = job.get("PrimaryLocation", "")
            if not any(z in loc.lower() for z in VAUD_ZONE):
                continue
            if is_relevant(title, short_desc):
                url = f"https://offres-emploi.vd.ch/#fr/job/{jid}"
                jobs.append({
                    "title": title,
                    "company": "État de Vaud",
                    "url": url,
                    "source": "offres-emploi.vd.ch",
                    "location": loc,
                    "found_at": datetime.now().isoformat(),
                })
                found += 1
        log(f"offres-emploi.vd.ch: {found} offre(s) trouvée(s) sur {len(reqs)} total")
    except Exception as e:
        log(f"Erreur scrape_vaud: {e}")
    return jobs


def scrape_jobscout24() -> list:
    """Offres privées via JobScout24.ch (secteur privé, éditeurs, bibliothèques…)."""
    KEYWORDS_JS24 = [
        "redacteur",
        "editeur",
        "bibliothecaire",
        "libraire",
        "correcteur",
        "traducteur",
        "documentaliste",
        "journaliste",
        "communication",
        "edition",
        "professeur-francais",
        "enseignant",
        "mediateur-culturel",
        "charge-de-communication",
        "archiviste",
        "musee",
        "patrimoine",
        "mediation",
        "charge-de-projet-culturel",
    ]

    BASE = "https://www.jobscout24.ch"
    jobs = []
    seen_urls: set = set()
    total_found = 0

    search_configs = [
        ("GE", GENEVE_ZONE),
        ("VD", VAUD_ZONE),
    ]

    for kw in KEYWORDS_JS24:
        for region_code, zone_filter in search_configs:
            url = f"{BASE}/fr/jobs/{kw}/?region={region_code}"
            try:
                r = SESSION.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                items = soup.select("li.job-list-item")
                for item in items:
                    title_el = item.select_one("a.job-link-detail")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href = title_el.get("href", "")
                    if not href:
                        continue
                    full_url = BASE + href if href.startswith("/") else href
                    if full_url in seen_urls:
                        continue

                    spans = item.select("p.job-attributes span")
                    company = spans[0].get_text(strip=True) if len(spans) > 0 else "—"
                    location = spans[1].get_text(strip=True) if len(spans) > 1 else "—"
                    loc_lower = location.lower()

                    if not any(z in loc_lower for z in zone_filter):
                        continue

                    if not is_relevant(title):
                        continue

                    seen_urls.add(full_url)
                    jobs.append({
                        "title": title,
                        "company": company,
                        "url": full_url,
                        "source": "jobscout24.ch",
                        "location": location if location else "—",
                        "found_at": datetime.now().isoformat(),
                    })
                    total_found += 1
            except Exception as e:
                log(f"Erreur jobscout24 [{kw}/{region_code}]: {e}")
            time.sleep(0.5)

    log(f"jobscout24.ch: {total_found} offre(s) trouvée(s)")
    return jobs


def scrape_jobup() -> list:
    """Offres secteur privé via jobup.ch (leader romand de l'emploi).

    La page de résultats est server-side rendered — pas besoin de JS.
    """
    BASE = "https://www.jobup.ch"

    KEYWORDS_JU = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire",
        "correcteur", "traducteur", "journaliste", "communication",
        "documentaliste", "archiviste", "bibliothèque", "édition",
        "professeur français", "enseignant français",
        "médiateur culturel", "chargé de projet culturel",
        "musée", "patrimoine", "médiation culturelle",
    ]

    SEARCH_CONFIGS = [
        ("region=34", None),
        ("location=nyon", VAUD_ZONE),
    ]

    jobs = []
    seen_urls: set = set()
    total = 0

    for kw in KEYWORDS_JU:
        for geo_param, zone_filter in SEARCH_CONFIGS:
            url = f"{BASE}/fr/emplois/?{geo_param}&term={quote(kw)}"
            try:
                r = SESSION.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                cards = soup.select("[data-cy='serp-item']")
                for card in cards:
                    link = card.select_one("[data-cy='job-link']")
                    if not link:
                        continue
                    title = link.get("title", "").strip()
                    href = link.get("href", "")
                    if not title or not href:
                        continue
                    full_url = BASE + href if href.startswith("/") else href
                    if full_url in seen_urls:
                        continue

                    card_text = card.get_text("\n")
                    loc = "Genève"
                    if "Lieu de travail" in card_text:
                        after = card_text.split("Lieu de travail", 1)[1]
                        loc_raw = after.strip().lstrip(":").split("\n")[0].strip()
                        if loc_raw:
                            loc = loc_raw[:60]

                    if zone_filter is not None:
                        if not any(z in loc.lower() for z in zone_filter):
                            continue

                    if not is_relevant(title):
                        continue

                    seen_urls.add(full_url)
                    jobs.append({
                        "title": title,
                        "company": "—",
                        "url": full_url,
                        "source": "jobup.ch",
                        "location": loc,
                        "found_at": datetime.now().isoformat(),
                    })
                    total += 1
            except Exception as e:
                log(f"Erreur jobup [{kw}/{geo_param}]: {e}")
            time.sleep(0.8)

    log(f"jobup.ch: {total} offre(s) trouvée(s)")
    return jobs


def scrape_adzuna() -> list:
    """Offres via l'API Adzuna — agrège Indeed, LinkedIn, JobScout, et +200 boards.

    Si ADZUNA_ID est vide, le scraper est silencieusement désactivé.
    """
    if not ADZUNA_ID or not ADZUNA_KEY:
        return []

    KEYWORDS_AZ = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire",
        "correcteur", "traducteur", "journaliste", "documentaliste",
        "professeur français", "communication culturelle", "archiviste",
        "médiateur culturel", "chargé de projet culturel",
    ]

    jobs = []
    seen_urls: set = set()
    total = 0

    for kw in KEYWORDS_AZ:
        url = (
            "https://api.adzuna.com/v1/api/jobs/ch/search/1"
            f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
            f"&results_per_page=50&what={quote(kw)}"
            "&where=Geneva&distance=30&max_days_old=30"
            "&content-type=application/json"
        )
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code != 200:
                log(f"Adzuna [{kw}]: HTTP {r.status_code}")
                continue
            for item in r.json().get("results", []):
                title = item.get("title", "").strip()
                link = item.get("redirect_url", "")
                desc = item.get("description", "")[:300]
                company = item.get("company", {}).get("display_name", "—")
                location = item.get("location", {}).get("display_name", "—")
                dedup_key = urlparse(link).path
                if not title or not link or dedup_key in seen_urls:
                    continue
                if not is_relevant(title, desc):
                    continue
                seen_urls.add(dedup_key)
                jobs.append({
                    "title": title,
                    "company": company,
                    "url": link,
                    "source": "Adzuna (Indeed+)",
                    "location": location,
                    "found_at": datetime.now().isoformat(),
                })
                total += 1
        except Exception as e:
            log(f"Adzuna [{kw}]: {e}")
        time.sleep(1)

    log(f"Adzuna: {total} offre(s) trouvée(s)")
    return jobs


INDEED_QUERIES = [
    ("rédacteur", "Genève"), ("éditeur", "Genève"),
    ("correcteur", "Genève"), ("bibliothécaire", "Genève"),
    ("traducteur", "Genève"), ("médiateur culturel", "Genève"),
    ("archiviste", "Genève"), ("journaliste", "Genève"),
    ("chargé de projet culturel", "Genève"),
]


def scrape_indeed_pw() -> list:
    """Offres Indeed CH via Playwright (vrai navigateur Chromium).

    Nécessite un Chromium système : sudo snap install chromium
    Si absent, la fonction retourne [] silencieusement.
    """
    if not PLAYWRIGHT_AVAILABLE:
        log("Indeed : Chromium système introuvable — source ignorée (sudo snap install chromium)")
        return []

    jobs = []
    seen_urls: set = set()
    total = 0

    with _sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_CHROMIUM_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="fr-CH",
        )
        page = ctx.new_page()

        for term, loc in INDEED_QUERIES:
            url = f"https://ch-fr.indeed.com/emplois?q={quote(term)}&l={quote(loc)}&radius=30"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)
                soup = BeautifulSoup(page.content(), "lxml")
                for card in soup.select("div.job_seen_beacon"):
                    title_el = (
                        card.select_one("h2.jobTitle span[title]")
                        or card.select_one("h2.jobTitle a span")
                    )
                    link_el = card.select_one("h2.jobTitle a")
                    loc_el = card.select_one("div.companyLocation")
                    if not title_el or not link_el:
                        continue
                    title = (title_el.get("title") or title_el.get_text(strip=True)).strip()
                    href = link_el.get("href", "")
                    if "/voir-emploi" not in href and "jk=" not in href:
                        continue
                    full_url = "https://ch-fr.indeed.com" + href if href.startswith("/") else href
                    if full_url in seen_urls:
                        continue
                    location = loc_el.get_text(strip=True) if loc_el else "Genève"
                    if not is_relevant(title):
                        continue
                    seen_urls.add(full_url)
                    jobs.append({
                        "title": title,
                        "company": "—",
                        "url": full_url,
                        "source": "Indeed CH",
                        "location": location,
                        "found_at": datetime.now().isoformat(),
                    })
                    total += 1
            except Exception as e:
                log(f"Indeed PW [{term}]: {e}")
            time.sleep(2)

        browser.close()

    log(f"Indeed CH (Playwright): {total} offre(s) trouvée(s)")
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
                "location": "Genève",
                "found_at": datetime.now().isoformat(),
            })

    log(f"ge.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Alertes email
# ---------------------------------------------------------------------------

def send_alert(new_jobs: list):
    """Envoie un récapitulatif par email si de nouvelles offres ont été trouvées."""
    if not new_jobs or not SMTP_PASS or not SMTP_FROM:
        return
    body = "\n".join(
        f"- {j['title']} ({j.get('source', '?')})\n  {j['url']}"
        for j in new_jobs
    )
    msg = MIMEText(f"{len(new_jobs)} nouvelle(s) offre(s) :\n\n{body}")
    msg["Subject"] = f"[find_job] {len(new_jobs)} nouvelle(s) offre(s)"
    msg["From"] = SMTP_FROM
    msg["To"] = "alexlarmeg@gmail.com"
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
        log(f"Email envoyé : {len(new_jobs)} offre(s) → alexlarmeg@gmail.com")
    except Exception as e:
        log(f"Erreur email : {e}")


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
                f'<td>{j.get("company", "—")}</td>'
                f'<td>{j.get("location", "—")}</td>'
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
            f'<th>Lieu</th><th>Source</th><th>Trouvé le</th></tr></thead>'
            f'<tbody>{rows(new_jobs, "new")}</tbody></table>'
        )
    )

    sorted_all = sorted(all_jobs, key=lambda x: x["found_at"], reverse=True)
    section_all = (
        "<p>Aucune offre trouvée.</p>"
        if not all_jobs
        else (
            f'<table><thead><tr><th>Poste</th><th>Entreprise</th>'
            f'<th>Lieu</th><th>Source</th><th>Trouvé le</th></tr></thead>'
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
    with open(PUBLIC_FILE, "w", encoding="utf-8") as f:
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

    # Purger les offres expirées avant d'ajouter les nouvelles
    all_jobs, seen = expire_old_jobs(all_jobs, seen)

    raw = []
    raw.extend(scrape_ville_geneve())
    raw.extend(scrape_letemps())
    raw.extend(scrape_ge_ch())
    raw.extend(scrape_vaud())
    raw.extend(scrape_jobscout24())
    raw.extend(scrape_jobup())
    raw.extend(scrape_indeed_pw())
    raw.extend(scrape_adzuna())

    # Déduplication : par ID (titre+URL) ET par (titre+entreprise) pour les doublons inter-sources
    seen_tc = {
        f"{j['title'].lower().strip()}|{j.get('company', '').lower().strip()}"
        for j in all_jobs
    }

    new_jobs = []
    for job in raw:
        jid = job_id(job["title"], job["url"])
        tc_key = f"{job['title'].lower().strip()}|{job.get('company', '').lower().strip()}"
        if jid not in seen and tc_key not in seen_tc:
            seen.add(jid)
            seen_tc.add(tc_key)
            new_jobs.append(job)
            all_jobs.append(job)

    log(f"Nouvelles offres : {len(new_jobs)} | Total cumulé : {len(all_jobs)}")
    save_seen(seen)
    save_all_jobs(all_jobs)
    generate_html(new_jobs, all_jobs)
    send_alert(new_jobs)
    log("=== Recherche terminée ===\n")


if __name__ == "__main__":
    main()
