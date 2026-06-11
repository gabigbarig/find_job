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
DOCS_DIR = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
PUBLIC_FILE = DOCS_DIR / "index.html"
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
]

# Communes et lieux du district de Nyon (Vaud) acceptables pour le zone Genève–Gland
VAUD_ZONE = {
    "nyon", "gland", "coppet", "prangins", "rolle",
    "mies", "tannay", "commugny", "founex", "bogis",
    "chavannes-de-bogis", "chavannes-des-bois", "borex",
    "eysins", "signy", "genolier", "gingins", "givrins",
    "arzier", "saint-cergue", "coinsins", "vich", "grens",
    "dully", "luins", "gilly", "tartegnin", "perroy",
    "allaman", "aubonne",
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
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            log(f"Erreur fetch {url} (tentative {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_jura() -> list:
    """Offres du Canton du Jura — enseignement et administration.

    La page Jura liste les offres dans des <table> (titre en texte brut)
    et fournit un lien PDF séparé pour chaque offre via <a href="...Htdocs...">
    Le lien PDF a le même texte que le titre de l'offre.
    """
    import re as _re

    jobs = []
    pages = [
        (
            "https://www.jura.ch/fr/Autorites/Administration/DFI/SRH/"
            "Offres-d-emploi/Offres-d-emploi-Enseignement.html",
            "jura.ch (enseignement)",
        ),
        (
            "https://www.jura.ch/fr/Autorites/Administration/DFI/SRH/"
            "Offres-d-emploi-Administration/Offres-d-emploi-Administration.html",
            "jura.ch (administration)",
        ),
        (
            "https://www.jura.ch/fr/Autorites/Administration/DFI/SRH/"
            "Offres-d-emploi/Offres-d-emploi-Autres.html",
            "jura.ch (autres)",
        ),
    ]

    for page_url, source_label in pages:
        soup = fetch(page_url)
        if not soup:
            continue

        # Les liens PDF ont href contenant "/Htdocs/" et le texte = titre de l'offre
        found_on_page = 0
        for a in soup.select("a[href*='/Htdocs/']"):
            href = a["href"]
            if not href.startswith("http"):
                href = "https://www.jura.ch" + href
            # Nettoyer le titre : supprimer "(PDF, X Ko)" à la fin
            raw_title = a.get_text(strip=True)
            title = _re.sub(r"\s*\(PDF[^)]*\)\s*$", "", raw_title).strip()
            if not title:
                continue
            if is_relevant(title):
                jobs.append({
                    "title": title,
                    "company": "Canton du Jura",
                    "url": href,
                    "source": source_label,
                    "found_at": datetime.now().isoformat(),
                })
                found_on_page += 1

        time.sleep(1)
        log(f"{source_label}: {found_on_page} offre(s) trouvée(s)")

    return jobs


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

    # Dédoublonner par URL (même lien peut apparaître deux fois dans la page)
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
        if title and href and is_relevant(title):
            jobs.append({
                "title": title,
                "company": company,
                "url": href,
                "source": "Le Temps Emploi",
                "location": "—",
                "found_at": datetime.now().isoformat(),
            })

    log(f"Le Temps Emploi: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_vaud() -> list:
    """Offres de l'État de Vaud via Oracle HCM REST API.

    Le portail offres-emploi.vd.ch délègue à Oracle Cloud HCM.
    L'API REST est publiquement accessible et retourne du JSON.
    """
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
        r = requests.get(api_url, headers=headers_oracle, timeout=20)
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


def scrape_fribourg() -> list:
    """Offres de l'État de Fribourg via jobs.fr.ch.

    La page de recherche retourne des div.job avec un lien dont le texte
    est le titre du poste et le href est le chemin relatif vers l'offre.
    """
    jobs = []
    url = "https://jobs.fr.ch/search/?createNewAlert=false&q=&locationsearch=&optionsFacets"
    soup = fetch(url)
    if not soup:
        return jobs

    found = 0
    seen_urls: set = set()
    for div in soup.select("div.job"):
        # Le premier lien dans le div porte le titre du poste
        link = div.select_one("a[href]")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue
        full_url = "https://jobs.fr.ch" + href if href.startswith("/") else href
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        if is_relevant(title):
            jobs.append({
                "title": title,
                "company": "État de Fribourg",
                "url": full_url,
                "source": "jobs.fr.ch",
                "found_at": datetime.now().isoformat(),
            })
            found += 1

    log(f"jobs.fr.ch (Fribourg): {found} offre(s) trouvée(s)")
    return jobs


def scrape_lausanne() -> list:
    """Offres de la Ville de Lausanne via Oracle HCM REST API (siteNumber=CX_1, même tenant que Vaud)."""
    jobs = []
    oracle_base = "https://fa-ewrg-saasfaeuraprod1.fa.ocs.oraclecloud.com"
    api_url = (
        f"{oracle_base}/hcmRestApi/resources/11.13.18.05/"
        "recruitingCEJobRequisitions"
        "?expand=requisitionList"
        "&finder=findReqs;siteNumber=CX_1,limit=500"
    )
    headers_oracle = {
        **HEADERS,
        "Accept": "application/json",
        "ora-irc-vanity-domain": "Y",
    }
    try:
        r = requests.get(api_url, headers=headers_oracle, timeout=20)
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
            if is_relevant(title, short_desc):
                url = (
                    "https://www.lausanne.ch/officiel/travailler-a-la-ville/"
                    f"nous-rejoindre/offres-emploi/detail-offre-emploi.html?id={jid}"
                )
                jobs.append({
                    "title": title,
                    "company": "Ville de Lausanne",
                    "url": url,
                    "source": "offres-emploi.lausanne.ch",
                    "found_at": datetime.now().isoformat(),
                })
                found += 1
        log(f"offres-emploi.lausanne.ch: {found} offre(s) trouvée(s) sur {len(reqs)} total")
    except Exception as e:
        log(f"Erreur scrape_lausanne: {e}")
    return jobs


def scrape_bcu_lausanne() -> list:
    """Offres de la Bibliothèque Cantonale et Universitaire de Lausanne (WordPress)."""
    jobs = []
    url = "https://www.bcu-lausanne.ch/emplois/"
    soup = fetch(url)
    if not soup:
        return jobs

    found = 0
    for article in soup.select("article.job-item"):
        title_el = article.select_one("h2 a, h3 a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        if not title or not href:
            continue
        if is_relevant(title):
            jobs.append({
                "title": title,
                "company": "BCU Lausanne",
                "url": href,
                "source": "bcu-lausanne.ch",
                "found_at": datetime.now().isoformat(),
            })
            found += 1

    log(f"bcu-lausanne.ch: {found} offre(s) trouvée(s)")
    return jobs


def scrape_jobscout24() -> list:
    """Offres privées via JobScout24.ch (secteur privé, éditeurs, bibliothèques…).

    Cherche plusieurs termes-clés dans la région de Genève (GE) et le district
    de Nyon (VD), afin de capturer les employeurs privés comme les maisons d'édition.
    """
    # Termes de recherche pertinents pour Lettres Modernes (slugs URL JobScout24)
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
    ]

    # Localisations acceptées pour les résultats VD (district de Nyon)
    VAUD_ZONE_LOWER = VAUD_ZONE  # already lowercase strings

    # Communes du canton de Genève (pour filtrage secondaire si nécessaire)
    GENEVE_ZONE = {
        "genève", "geneva", "carouge", "lancy", "meyrin", "vernier", "onex",
        "plan-les-ouates", "thônex", "bernex", "chêne-bougeries", "chêne-bourg",
        "pregny-chambésy", "grand-saconnex", "satigny", "dardagny", "russin",
        "avully", "avusy", "cartigny", "chancy", "laconnex", "soral", "gy",
        "jussy", "choulex", "cologny", "vandoeuvres", "puplinge", "presinge",
        "meinier", "collonge-bellerive", "hermance", "anières", "corsier",
        "céligny", "bellevue", "genthod", "versoix", "collex-bossy",
    }

    BASE = "https://www.jobscout24.ch"
    jobs = []
    seen_urls: set = set()
    total_found = 0

    search_configs = [
        ("GE", GENEVE_ZONE),      # Canton Genève → communes du canton
        ("VD", VAUD_ZONE_LOWER),  # Vaud → seulement district Nyon
    ]

    for kw in KEYWORDS_JS24:
        for region_code, zone_filter in search_configs:
            url = f"{BASE}/fr/jobs/{kw}/?region={region_code}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
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

                    # Filtre géographique pour Vaud
                    if zone_filter is not None:
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
    Recherche avec region=34 (canton de Genève) et location=nyon pour Nyon/Gland.
    """
    import urllib.parse

    BASE = "https://www.jobup.ch"

    KEYWORDS_JU = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire",
        "correcteur", "traducteur", "journaliste", "communication",
        "documentaliste", "archiviste", "bibliothèque", "édition",
        "professeur français", "enseignant français",
    ]

    # Zones géographiques : region=34 (GE) + location=nyon pour district Nyon
    SEARCH_CONFIGS = [
        ("region=34", None),           # Canton de Genève
        ("location=nyon", VAUD_ZONE),  # Nyon et district, filtre VAUD_ZONE
    ]

    jobs = []
    seen_urls: set = set()
    total = 0

    for kw in KEYWORDS_JU:
        for geo_param, zone_filter in SEARCH_CONFIGS:
            url = f"{BASE}/fr/emplois/?{geo_param}&term={urllib.parse.quote(kw)}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                if r.status_code != 200:
                    continue
                from bs4 import BeautifulSoup as _BS
                soup = _BS(r.text, "lxml")
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

                    # Extraire la localisation du texte de la carte
                    card_text = card.get_text("\n")
                    loc = "Genève"
                    if "Lieu de travail" in card_text:
                        after = card_text.split("Lieu de travail", 1)[1]
                        loc_raw = after.strip().lstrip(":").split("\n")[0].strip()
                        if loc_raw:
                            loc = loc_raw[:60]

                    # Filtre géographique pour Nyon (Vaud)
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


def scrape_duckduckgo() -> list:
    """Méta-recherche DuckDuckGo pour attraper des offres hors portails connus.

    DuckDuckGo HTML (pas de JS) agrège des offres depuis tous les sites
    indexés, y compris les sites d'entreprises avec structured data,
    les boards de niche, etc. — comme ferait Google.
    """
    DDG_URL = "https://html.duckduckgo.com/html/"

    # Requêtes ciblées : terme + zone géographique
    DDG_QUERIES = [
        "rédacteur emploi Genève Suisse",
        "éditeur offre emploi Genève",
        "bibliothécaire emploi Genève Nyon",
        "libraire offre emploi Genève CDI",
        "traducteur emploi Genève Suisse romande",
        "correcteur offre emploi Suisse romande",
        "documentaliste emploi Genève",
        "professeur français emploi Genève école",
        "chargé communication emploi Genève",
        "archiviste emploi Genève Suisse",
    ]

    # Domaines de job boards à exclure (on les scrape déjà directement)
    ALREADY_COVERED = {"ge.ch", "ville-geneve.ch", "jobscout24.ch", "vd.ch"}

    # Mots indicateurs qu'un résultat est bien une offre d'emploi individuelle
    JOB_INDICATORS = ["emploi", "job", "offre", "poste", "recrutement",
                      "career", "vacancy", "emplois", "stellenangebote"]

    GEO_ZONE = {
        "genève", "geneva", "nyon", "gland", "carouge", "meyrin",
        "vernier", "lancy", "bernex", "grand-saconnex", "rolle",
        "coppet", "suisse romande", "suisseromande",
    }

    jobs = []
    seen_urls: set = set()
    total = 0

    for query in DDG_QUERIES:
        try:
            r = requests.post(
                DDG_URL,
                data={"q": query, "kl": "ch-fr"},
                headers=HEADERS,
                timeout=15,
            )
            if r.status_code != 200:
                continue
            from bs4 import BeautifulSoup as _BS
            soup = _BS(r.text, "lxml")
            for res in soup.select(".result__body"):
                title_el = res.select_one(".result__a")
                snippet_el = res.select_one(".result__snippet")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                href = title_el.get("href", "")

                # Exclure les publicités DDG (href contient "ad_domain")
                if "ad_domain" in href or "ad_provider" in href:
                    continue
                # Garder seulement les URLs qui ressemblent à des offres
                if not any(ind in href.lower() for ind in JOB_INDICATORS):
                    continue
                # Exclure les boards déjà couverts
                if any(d in href for d in ALREADY_COVERED):
                    continue
                if href in seen_urls:
                    continue

                combined = (title + " " + snippet).lower()
                # Filtre pertinence + zone géographique
                if not is_relevant(title, snippet):
                    continue
                if not any(z in combined for z in GEO_ZONE):
                    continue

                seen_urls.add(href)
                jobs.append({
                    "title": title,
                    "company": "—",
                    "url": href,
                    "source": "DuckDuckGo",
                    "location": "—",
                    "found_at": datetime.now().isoformat(),
                })
                total += 1
        except Exception as e:
            log(f"Erreur DuckDuckGo [{query[:30]}]: {e}")
        time.sleep(2)

    log(f"DuckDuckGo: {total} offre(s) trouvée(s)")
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

    raw = []
    raw.extend(scrape_ville_geneve())
    raw.extend(scrape_letemps())
    raw.extend(scrape_ge_ch())
    raw.extend(scrape_vaud())
    raw.extend(scrape_jobscout24())
    raw.extend(scrape_jobup())
    raw.extend(scrape_duckduckgo())

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
