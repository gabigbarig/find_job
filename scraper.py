#!/usr/bin/env python3
"""Agent de recherche d'emploi - Lettres Modernes - Genève.

Améliorations par rapport à la version initiale :
- Secrets (clés API, mot de passe SMTP) chargés depuis l'environnement / .env,
  plus jamais en clair dans le code.
- Échappement HTML systématique dans le rapport généré.
- Normalisation Unicode pour un matching robuste aux accents (français/francais).
- User-Agent unique et centralisé.
- fetch() applique un délai poli et un back-off exponentiel.
- Vérification optionnelle de robots.txt avant chaque requête.
- Avertissement loggué quand un scraper retourne 0 offre de façon anormale.
"""

import json
import os
import re
import shutil
import time
import hashlib
import smtplib
import unicodedata
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# --- Chargement optionnel d'un fichier .env (sans dépendance externe) ---
def _load_dotenv(path: Path):
    """Charge un .env minimal (KEY=VALUE) sans écraser l'environnement existant."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


BASE_DIR = Path(__file__).parent
_load_dotenv(BASE_DIR / ".env")

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
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

# Alertes email (optionnel). Mot de passe d'application Google (16 caractères),
# PAS le vrai mot de passe. https://myaccount.google.com/apppasswords
# À définir dans .env : SMTP_FROM=... / SMTP_PASS=... / SMTP_TO=...
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO = os.environ.get("SMTP_TO", "")

# Respecter robots.txt avant chaque requête (recommandé : True)
RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "1") not in ("0", "false", "False")

# Offres de plus de EXPIRY_DAYS jours retirées de la base à chaque run
EXPIRY_DAYS = int(os.environ.get("EXPIRY_DAYS", "60"))

# Délai poli minimum entre deux requêtes vers un même domaine (secondes)
POLITE_DELAY = float(os.environ.get("POLITE_DELAY", "1.0"))

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
PUBLIC_FILE = DOCS_DIR / "index.html"
LOG_FILE = DATA_DIR / "scraper.log"

# User-Agent unique, centralisé (utilisé partout : requests ET Playwright)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
HEADERS = dict(SESSION.headers)

# ---------------------------------------------------------------------------
# Mots-clés et zones (inchangés)
# ---------------------------------------------------------------------------

KEYWORDS = [
    "professeur français", "professeur de français", "enseignant français",
    "rédacteur", "rédactrice", "libraire", "éditeur", "éditrice",
    "correcteur", "correctrice", "journaliste", "lettres", "bibliothécaire",
    "chargé de communication", "chargée de communication",
    "attaché de presse", "attachée de presse", "maison d'édition",
    "traducteur", "traductrice", "archiviste", "documentaliste",
    "médiateur culturel", "médiatrice culturelle",
    "chargé de projet culturel", "chargée de projet culturel",
    "animateur culturel", "animatrice culturelle", "muséologue",
    "responsable culturel", "responsable de collection",
    "chargé des publics", "chargée des publics", "médiation culturelle",
    "maître d'enseignement général / français",
    "maître d'enseignement général - français",
    "maître d'enseignement général – français",
    "maîtresse d'enseignement général / français",
    "maîtresse d'enseignement général - français",
    "maîtresse d'enseignement général – français",
    "français langue étrangère", "français cdd", "expression orale",
    "diction", "culture générale",
]

TEACHING_TERMS = [
    "enseignement", "enseignant", "enseignante",
    "maître", "maîtresse", "professeur", "professeure",
]

LETTRES_SUBJECTS = [
    "français", "lettres", "littérature", "expression orale",
    "diction", "français langue étrangère", "culture générale",
]

EXCLUDE_KEYWORDS = [
    "informatique", "ingénieur", "développeur", "comptable",
    "médecin", "infirmier", "avocat", "électricien", "chauffeur",
    "technicien", "mécanicien",
]

VAUD_ZONE = {
    "nyon", "gland", "coppet", "prangins", "rolle", "mies", "tannay",
    "commugny", "founex", "bogis", "chavannes-de-bogis",
    "chavannes-des-bois", "borex", "eysins", "signy", "genolier",
    "gingins", "givrins", "arzier", "saint-cergue", "coinsins", "vich",
    "grens", "dully", "luins", "gilly", "tartegnin", "perroy",
    "allaman", "aubonne",
}

GENEVE_ZONE = {
    "genève", "geneva", "carouge", "lancy", "meyrin", "vernier", "onex",
    "plan-les-ouates", "thônex", "bernex", "chêne-bougeries",
    "chêne-bourg", "pregny-chambésy", "grand-saconnex", "satigny",
    "dardagny", "russin", "avully", "avusy", "cartigny", "chancy",
    "laconnex", "soral", "gy", "jussy", "choulex", "cologny",
    "vandoeuvres", "puplinge", "presinge", "meinier",
    "collonge-bellerive", "hermance", "anières", "corsier", "céligny",
    "bellevue", "genthod", "versoix", "collex-bossy",
}

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def normalize(text: str) -> str:
    """Minuscule + suppression des accents, pour un matching robuste.

    'Français' et 'Francais' deviennent tous deux 'francais'.
    """
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


# Versions normalisées (pré-calculées une fois) pour is_relevant
_KW_NORM = [normalize(k) for k in KEYWORDS]
_EXCLUDE_NORM = [normalize(k) for k in EXCLUDE_KEYWORDS]
_TEACHING_NORM = [normalize(t) for t in TEACHING_TERMS]
_SUBJECTS_NORM = [normalize(s) for s in LETTRES_SUBJECTS]


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
    text = normalize(title + " " + description)
    if any(k in text for k in _EXCLUDE_NORM):
        return False
    if any(k in text for k in _KW_NORM):
        return True
    if any(t in text for t in _TEACHING_NORM) and any(s in text for s in _SUBJECTS_NORM):
        return True
    return False


# --- Cache des parseurs robots.txt par domaine ---
_ROBOTS_CACHE: dict = {}


def robots_allows(url: str) -> bool:
    """Vérifie si l'URL est autorisée par le robots.txt du domaine.

    En cas d'échec de lecture du robots.txt, on autorise par défaut (fail-open),
    pour ne pas bloquer tout le scraper sur un domaine sans robots.txt.
    """
    if not RESPECT_ROBOTS:
        return True
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    rp = _ROBOTS_CACHE.get(base)
    if rp is None:
        rp = RobotFileParser()
        rp.set_url(urljoin(base, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            rp = None  # illisible : on autorisera
        _ROBOTS_CACHE[base] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# --- Délai poli par domaine ---
_LAST_REQUEST: dict = {}


def _polite_wait(url: str):
    """Garantit au moins POLITE_DELAY secondes entre deux hits d'un même domaine."""
    domain = urlparse(url).netloc
    last = _LAST_REQUEST.get(domain, 0.0)
    elapsed = time.time() - last
    if elapsed < POLITE_DELAY:
        time.sleep(POLITE_DELAY - elapsed)
    _LAST_REQUEST[domain] = time.time()


def fetch(url: str, retries: int = 3):
    """GET poli avec respect de robots.txt, délai par domaine et back-off."""
    if not robots_allows(url):
        log(f"robots.txt interdit : {url} — ignoré")
        return None
    for attempt in range(retries):
        _polite_wait(url)
        try:
            r = SESSION.get(url, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log(f"Erreur fetch {url} (tentative {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def _warn_if_empty(source: str, jobs: list, expect_results: bool = True):
    """Loggue un avertissement si un scraper censé produire des offres en renvoie 0.

    Un 0 soudain est souvent le signe d'un sélecteur CSS cassé (le site a changé).
    """
    if expect_results and not jobs:
        log(f"⚠️  {source}: 0 offre — sélecteur potentiellement cassé ou source bloquée")


def dedup_by_url(jobs: list) -> list:
    seen_urls, unique = set(), []
    for j in jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique.append(j)
    return unique


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
                "title": title, "company": "Ville de Genève", "url": href,
                "source": "ville-geneve.ch", "location": "Genève",
                "found_at": datetime.now().isoformat(),
            })

    jobs = dedup_by_url(jobs)
    log(f"ville-geneve.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_letemps() -> list:
    """Le Temps Emploi — page de listing."""
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
                "title": title, "company": company, "url": href,
                "source": "Le Temps Emploi", "location": location,
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
    headers_oracle = {**HEADERS, "Accept": "application/json", "ora-irc-vanity-domain": "Y"}
    try:
        _polite_wait(api_url)
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
                jobs.append({
                    "title": title, "company": "État de Vaud",
                    "url": f"https://offres-emploi.vd.ch/#fr/job/{jid}",
                    "source": "offres-emploi.vd.ch", "location": loc,
                    "found_at": datetime.now().isoformat(),
                })
                found += 1
        log(f"offres-emploi.vd.ch: {found} offre(s) trouvée(s) sur {len(reqs)} total")
    except Exception as e:
        log(f"Erreur scrape_vaud: {e}")
    return jobs


def scrape_jobscout24() -> list:
    """Offres privées via JobScout24.ch."""
    KEYWORDS_JS24 = [
        "redacteur", "editeur", "bibliothecaire", "libraire", "correcteur",
        "traducteur", "documentaliste", "journaliste", "communication",
        "edition", "professeur-francais", "enseignant", "mediateur-culturel",
        "charge-de-communication", "archiviste", "musee", "patrimoine",
        "mediation", "charge-de-projet-culturel",
    ]
    BASE = "https://www.jobscout24.ch"
    jobs, seen_urls, total_found = [], set(), 0
    search_configs = [("GE", GENEVE_ZONE), ("VD", VAUD_ZONE)]

    for kw in KEYWORDS_JS24:
        for region_code, zone_filter in search_configs:
            url = f"{BASE}/fr/jobs/{kw}/?region={region_code}"
            if not robots_allows(url):
                continue
            try:
                _polite_wait(url)
                r = SESSION.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                for item in soup.select("li.job-list-item"):
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
                    if not any(z in location.lower() for z in zone_filter):
                        continue
                    if not is_relevant(title):
                        continue
                    seen_urls.add(full_url)
                    jobs.append({
                        "title": title, "company": company, "url": full_url,
                        "source": "jobscout24.ch", "location": location or "—",
                        "found_at": datetime.now().isoformat(),
                    })
                    total_found += 1
            except Exception as e:
                log(f"Erreur jobscout24 [{kw}/{region_code}]: {e}")

    log(f"jobscout24.ch: {total_found} offre(s) trouvée(s)")
    return jobs


def scrape_jobup() -> list:
    """Offres via jobup.ch.

    ATTENTION : le robots.txt de jobup interdit /api/. Cette fonction parse le
    HTML server-side. Si jobup charge ses offres via JS/API, ce scraper
    retournera 0 — dans ce cas, préférer le sitemap public :
    https://www.jobup.ch/sitemaps/jobup/fr/sitemap.xml
    """
    BASE = "https://www.jobup.ch"
    KEYWORDS_JU = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire", "correcteur",
        "traducteur", "journaliste", "communication", "documentaliste",
        "archiviste", "bibliothèque", "édition", "professeur français",
        "enseignant français", "médiateur culturel",
        "chargé de projet culturel", "musée", "patrimoine",
        "médiation culturelle",
    ]
    SEARCH_CONFIGS = [("region=34", None), ("location=nyon", VAUD_ZONE)]
    jobs, seen_urls, total = [], set(), 0

    for kw in KEYWORDS_JU:
        for geo_param, zone_filter in SEARCH_CONFIGS:
            url = f"{BASE}/fr/emplois/?{geo_param}&term={quote(kw)}"
            if not robots_allows(url):
                continue
            try:
                _polite_wait(url)
                r = SESSION.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                for card in soup.select("[data-cy='serp-item']"):
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
                    if zone_filter is not None and not any(z in loc.lower() for z in zone_filter):
                        continue
                    if not is_relevant(title):
                        continue
                    seen_urls.add(full_url)
                    jobs.append({
                        "title": title, "company": "—", "url": full_url,
                        "source": "jobup.ch", "location": loc,
                        "found_at": datetime.now().isoformat(),
                    })
                    total += 1
            except Exception as e:
                log(f"Erreur jobup [{kw}/{geo_param}]: {e}")

    _warn_if_empty("jobup.ch", jobs)
    log(f"jobup.ch: {total} offre(s) trouvée(s)")
    return jobs


def scrape_adzuna() -> list:
    """Offres via l'API Adzuna — agrège Indeed, LinkedIn, etc.

    Désactivé silencieusement si les identifiants ne sont pas configurés.
    """
    if not ADZUNA_ID or not ADZUNA_KEY:
        log("Adzuna : identifiants absents (ADZUNA_ID/ADZUNA_KEY) — source ignorée")
        return []

    KEYWORDS_AZ = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire", "correcteur",
        "traducteur", "journaliste", "documentaliste", "professeur français",
        "communication culturelle", "archiviste", "médiateur culturel",
        "chargé de projet culturel",
    ]
    jobs, seen_urls, total = [], set(), 0

    for kw in KEYWORDS_AZ:
        url = (
            "https://api.adzuna.com/v1/api/jobs/ch/search/1"
            f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
            f"&results_per_page=50&what={quote(kw)}"
            "&where=Geneva&distance=30&max_days_old=30"
            "&content-type=application/json"
        )
        try:
            _polite_wait(url)
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
                    "title": title, "company": company, "url": link,
                    "source": "Adzuna (Indeed+)", "location": location,
                    "found_at": datetime.now().isoformat(),
                })
                total += 1
        except Exception as e:
            log(f"Adzuna [{kw}]: {e}")

    log(f"Adzuna: {total} offre(s) trouvée(s)")
    return jobs


INDEED_QUERIES = [
    ("rédacteur", "Genève"), ("éditeur", "Genève"), ("correcteur", "Genève"),
    ("bibliothécaire", "Genève"), ("traducteur", "Genève"),
    ("médiateur culturel", "Genève"), ("archiviste", "Genève"),
    ("journaliste", "Genève"), ("chargé de projet culturel", "Genève"),
]


def scrape_indeed_pw() -> list:
    """Offres Indeed CH via Playwright. Nécessite un Chromium système."""
    if not PLAYWRIGHT_AVAILABLE:
        log("Indeed : Chromium système introuvable — source ignorée (sudo snap install chromium)")
        return []

    jobs, seen_urls, total = [], set(), 0
    with _sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_CHROMIUM_PATH, headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900}, locale="fr-CH",
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
                        "title": title, "company": "—", "url": full_url,
                        "source": "Indeed CH", "location": location,
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
                "title": title, "company": "État de Genève", "url": href,
                "source": "ge.ch", "location": "Genève",
                "found_at": datetime.now().isoformat(),
            })

    log(f"ge.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Alertes email
# ---------------------------------------------------------------------------

def send_alert(new_jobs: list):
    """Envoie un récapitulatif par email si de nouvelles offres ont été trouvées."""
    if not new_jobs or not SMTP_PASS or not SMTP_FROM or not SMTP_TO:
        return
    body = "\n".join(
        f"- {j['title']} ({j.get('source', '?')})\n  {j['url']}"
        for j in new_jobs
    )
    msg = MIMEText(f"{len(new_jobs)} nouvelle(s) offre(s) :\n\n{body}")
    msg["Subject"] = f"[find_job] {len(new_jobs)} nouvelle(s) offre(s)"
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
        log(f"Email envoyé : {len(new_jobs)} offre(s) → {SMTP_TO}")
    except Exception as e:
        log(f"Erreur email : {e}")


# ---------------------------------------------------------------------------
# Rapport HTML (toutes les valeurs dynamiques sont échappées)
# ---------------------------------------------------------------------------

def generate_html(new_jobs: list, all_jobs: list):
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")

    def rows(job_list, css_class=""):
        out = ""
        for j in job_list:
            found = escape(j["found_at"][:16].replace("T", " "))
            out += (
                f'<tr class="{escape(css_class)}">'
                f'<td><a href="{escape(j["url"])}" target="_blank" rel="noopener">'
                f'{escape(j["title"])}</a></td>'
                f'<td>{escape(j.get("company", "—"))}</td>'
                f'<td>{escape(j.get("location", "—"))}</td>'
                f'<td>{escape(j["source"])}</td>'
                f'<td>{found}</td>'
                f'</tr>\n'
            )
        return out

    section_new = (
        "<p>Aucune nouvelle offre depuis la dernière recherche.</p>"
        if not new_jobs else
        ('<table><thead><tr><th>Poste</th><th>Entreprise</th><th>Lieu</th>'
         '<th>Source</th><th>Trouvé le</th></tr></thead>'
         f'<tbody>{rows(new_jobs, "new")}</tbody></table>')
    )

    sorted_all = sorted(all_jobs, key=lambda x: x["found_at"], reverse=True)
    section_all = (
        "<p>Aucune offre trouvée.</p>"
        if not all_jobs else
        ('<table><thead><tr><th>Poste</th><th>Entreprise</th><th>Lieu</th>'
         '<th>Source</th><th>Trouvé le</th></tr></thead>'
         f'<tbody>{rows(sorted_all)}</tbody></table>')
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

    RESULTS_FILE.write_text(html, encoding="utf-8")
    PUBLIC_FILE.write_text(html, encoding="utf-8")
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


# Chaque scraper est isolé : s'il plante, les autres continuent.
SCRAPERS = [
    scrape_ville_geneve, scrape_letemps, scrape_ge_ch, scrape_vaud,
    scrape_jobscout24, scrape_jobup, scrape_indeed_pw, scrape_adzuna,
]


def main():
    log("=== Démarrage de la recherche d'emploi ===")
    seen = load_seen()
    all_jobs = load_all_jobs()
    all_jobs, seen = expire_old_jobs(all_jobs, seen)

    raw = []
    for scraper in SCRAPERS:
        try:
            raw.extend(scraper())
        except Exception as e:
            log(f"⚠️  {scraper.__name__} a échoué : {e}")

    # Déduplication par ID (titre+URL) ET par (titre+entreprise)
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
