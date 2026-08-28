#!/usr/bin/env python3
"""Agent de recherche d'emploi - Lettres Modernes - Genève & arc lémanique.

═══════════════════════════════════════════════════════════════════════════
AMÉLIORATIONS PALIER 1 (par rapport à la version précédente)
═══════════════════════════════════════════════════════════════════════════
1. LECTURE DES DESCRIPTIONS : pour les offres au titre ambigu, le scraper va
   chercher la page de détail et analyse le texte complet. Ne rate plus les
   titres « déguisés » (« Collaborateur scientifique » parlant en fait de FLE).
2. NOUVELLES SOURCES : UNIGE (jobs.unige.ch), myScience (agrège toutes les
   universités suisses), museums.ch, kultur-vermittlung.ch, educa.ch.
3. MOTS-CLÉS ÉLARGIS : genres neutres, métiers proches, terminologie scolaire
   suisse et culturelle.
4. AUTO-DIAGNOSTIC : chaque source enregistre son nombre d'offres dans un
   historique de santé (data/health.json) et alerte si une source qui
   produisait des résultats tombe brutalement à 0 (= sélecteur cassé).
5. DONNÉES ENRICHIES : description, taux d'activité (%) et score de pertinence
   stockés pour chaque offre ; tri par score dans le rapport.

Les améliorations 1, 3, 4, 5 ne dépendent d'AUCUN sélecteur : elles sont
fiables immédiatement. Les nouveaux scrapers (point 2) utilisent des
sélecteurs « best effort » : s'ils ne matchent pas, l'auto-diagnostic te le
signale, et chaque sélecteur est isolé en tête de fonction, facile à corriger.
═══════════════════════════════════════════════════════════════════════════
"""

import json
import os
import re
import shutil
import socket
import time
import argparse
import fcntl
import hashlib
import imaplib
import smtplib
import threading
import unicodedata
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from typing import Callable, Literal, TypedDict
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urljoin, urlunparse
from zoneinfo import ZoneInfo

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

LOCAL_TIMEZONE = ZoneInfo("Europe/Zurich")
SCRAPER_LOCK_FILE = Path(os.environ.get("TMPDIR") or "/tmp") / (
    f"find_job-scraper-{os.getuid()}.lock"
)


def local_now() -> datetime:
    """Heure locale explicite, identique en WSL et dans GitHub Actions."""
    return datetime.now(LOCAL_TIMEZONE)


def parse_local_datetime(value: str) -> datetime:
    """Lit une date ISO moderne ou historique et la ramène à Europe/Zurich."""
    parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE)


class Job(TypedDict, total=False):
    """Schéma commun sérialisable utilisé par tous les adaptateurs de sources."""
    title: str
    url: str
    company: str
    employer: str
    location: str
    description: str
    source: str
    found_at: str
    url_checked_at: str
    date_posted: str
    valid_through: str
    employment_type: str
    job_location_type: str
    salary: str
    external_id: str
    posting_id: str
    taux: str
    score: int
    review_reason: str


class Decision(TypedDict):
    destination: Literal["main", "review", "reject"]
    reason: str
    review_reasons: list[str]


class ScraperAlreadyRunning(RuntimeError):
    """Une autre exécution détient déjà le verrou global du projet."""


@contextmanager
def scraper_process_lock():
    """Protège aussi les lancements directs qui contournent run.sh."""
    if os.environ.get("FIND_JOB_LOCK_HELD") == "1":
        yield
        return
    SCRAPER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCRAPER_LOCK_FILE, "a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScraperAlreadyRunning(
                "Une recherche d'emploi est déjà en cours. Nouvelle exécution ignorée."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PlaywrightBrowserUnavailable(RuntimeError):
    """Playwright est présent, mais aucun Chromium compatible n'est installé."""


def _is_snap_chromium(path: str) -> bool:
    """Détecte Chromium Snap et ses wrappers, incompatibles avec certains WSL."""
    if not path:
        return False
    candidate = Path(path)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        resolved = candidate
    if (
        str(candidate).startswith("/snap/")
        or str(resolved).startswith("/snap/")
        or resolved == Path("/usr/bin/snap")
    ):
        return True
    try:
        # Sur Ubuntu, /usr/bin/chromium-browser est un petit script qui délègue
        # à /snap/bin/chromium. Lire uniquement les petits wrappers évite de
        # charger un véritable exécutable Chromium en mémoire.
        if candidate.is_file() and candidate.stat().st_size <= 64 * 1024:
            wrapper = candidate.read_text(encoding="utf-8", errors="ignore")
            return (
                "/snap/bin/chromium" in wrapper
                or "snap run chromium" in wrapper
            )
    except OSError:
        pass
    return False


def _is_executable_file(path: str) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def _find_system_chromium() -> str:
    """Trouve un Chrome/Chromium natif en ignorant les lanceurs Snap."""
    for executable in (
        "google-chrome-stable", "google-chrome", "chromium", "chromium-browser",
    ):
        candidate = shutil.which(executable)
        if (
            candidate
            and _is_executable_file(candidate)
            and not _is_snap_chromium(candidate)
        ):
            return candidate
    return ""


try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _CHROMIUM_PATH = _find_system_chromium()
    # Playwright sait utiliser soit un Chromium système, soit le navigateur
    # installé par `python -m playwright install chromium` (cas GitHub Actions).
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    _CHROMIUM_PATH = ""

# ---------------------------------------------------------------------------
# Configuration (secrets chargés depuis l'environnement / .env)
# ---------------------------------------------------------------------------

ADZUNA_ID = os.environ.get("ADZUNA_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_KEY", "")

SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO = os.environ.get("SMTP_TO", "")

# Import facultatif des alertes emploi LinkedIn reçues par email. Le scraper ne
# se connecte jamais à LinkedIn : il lit uniquement une boîte IMAP fournie par
# l'utilisateur et extrait les offres contenues dans les messages d'alerte.
LINKEDIN_IMAP_HOST = os.environ.get("LINKEDIN_IMAP_HOST", "")
LINKEDIN_IMAP_PORT = int(os.environ.get("LINKEDIN_IMAP_PORT") or "993")
LINKEDIN_IMAP_USER = os.environ.get("LINKEDIN_IMAP_USER", "")
LINKEDIN_IMAP_PASS = os.environ.get("LINKEDIN_IMAP_PASS", "")
LINKEDIN_IMAP_FOLDER = os.environ.get("LINKEDIN_IMAP_FOLDER") or "INBOX"
LINKEDIN_IMAP_DAYS = int(os.environ.get("LINKEDIN_IMAP_DAYS", "7"))
LINKEDIN_IMAP_MAX_MESSAGES = int(os.environ.get("LINKEDIN_IMAP_MAX_MESSAGES", "100"))
LINKEDIN_ALERT_DEFAULT_LOCATION = os.environ.get(
    "LINKEDIN_ALERT_DEFAULT_LOCATION", ""
).strip()
# Depuis novembre 2025, ReliefWeb refuse les noms d'application non approuvés.
# Ne jamais inventer de valeur par défaut : une configuration absente désactive
# proprement la source au lieu de produire un 403 pour chaque terme recherché.
RELIEFWEB_APPNAME = os.environ.get("RELIEFWEB_APPNAME", "").strip()

RESPECT_ROBOTS = os.environ.get("RESPECT_ROBOTS", "1") not in ("0", "false", "False")
EXPIRY_DAYS = int(os.environ.get("EXPIRY_DAYS", "60"))
POLITE_DELAY = float(os.environ.get("POLITE_DELAY", "1.0"))

# Score de pertinence minimal pour qu'une offre soit retenue (posture stricte).
# Un mot-clé dans le titre vaut 2 ; un mot-clé en description vaut 1. À 2, on
# écarte les titres génériques (« Collaborateur scientifique ») relevés par un
# seul mot-clé faible en description. Mettre 1 pour une posture plus tolérante.
MIN_SCORE = int(os.environ.get("MIN_SCORE", "2"))

# Activer la lecture des pages de détail pour les titres ambigus (point 1).
# Coûte une requête supplémentaire par offre « limite », mais récupère les
# offres au titre neutre. Désactivable via FETCH_DESCRIPTIONS=0.
FETCH_DESCRIPTIONS = os.environ.get("FETCH_DESCRIPTIONS", "1") not in ("0", "false", "False")
# Nombre max de pages de détail récupérées par run (garde-fou anti-explosion).
# Relevé à 80 : avec la parallélisation des scrapers, on peut lire plus de fiches
# ambiguës (meilleur recall des titres « déguisés ») sans alourdir le run.
MAX_DETAIL_FETCHES = int(os.environ.get("MAX_DETAIL_FETCHES", "80"))
FETCH_LOCAL_DETAILS = os.environ.get("FETCH_LOCAL_DETAILS", "1") not in ("0", "false", "False")
# Indeed est bloqué par un mur anti-bot persistant (renvoie 0) et coûte ~50 s via
# Playwright : désactivé par défaut. Réactivable avec ENABLE_INDEED=1 sans le retirer.
ENABLE_INDEED = os.environ.get("ENABLE_INDEED", "0") not in ("0", "false", "False")
# Budget dédié à l'extraction de l'employeur (lecture des pages de détail des
# offres nouvelles sans entreprise). Séparé pour ne pas concurrencer ci-dessus.
MAX_EMPLOYER_FETCHES = int(os.environ.get("MAX_EMPLOYER_FETCHES", "40"))
# Les fiches déjà lues restent réutilisables pendant une courte période. Cela
# évite de consommer le quota sur les mêmes URL à chaque passage.
DETAIL_CACHE_TTL_HOURS = int(os.environ.get("DETAIL_CACHE_TTL_HOURS", "48"))
MAX_DETAIL_CACHE_ENTRIES = int(os.environ.get("MAX_DETAIL_CACHE_ENTRIES", "1200"))
DEAD_LINK_CHECK_TTL_HOURS = int(os.environ.get("DEAD_LINK_CHECK_TTL_HOURS", "24"))

DATA_ROOT = BASE_DIR / "data"
DATA_ROOT.mkdir(exist_ok=True)
DOCS_ROOT = BASE_DIR / "docs"
DOCS_ROOT.mkdir(exist_ok=True)
DATA_DIR = DATA_ROOT
DOCS_DIR = DOCS_ROOT
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
PUBLIC_FILE = DOCS_DIR / "index.html"
LOG_FILE = DATA_DIR / "scraper.log"
HEALTH_FILE = DATA_DIR / "health.json"      # historique de santé des sources
RSS_FILE = DOCS_DIR / "feed.xml"            # flux RSS en sortie (bonus)
REVIEW_FILE = DATA_DIR / "review_jobs.json"
REJECTIONS_FILE = DATA_DIR / "rejections.json"
COVERAGE_FILE = DATA_DIR / "query_coverage.json"
DETAIL_CACHE_FILE = DATA_DIR / "detail_cache.json"
ATS_SOURCES_FILE = BASE_DIR / "ats_sources.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_SESSION_LOCAL = threading.local()
_RUN_HTML_CACHE: dict[str, str] = {}
_RUN_DEAD_URL_CACHE: dict[str, bool] = {}
_RUN_CACHE_LOCK = threading.Lock()
_RUN_CACHE_ENABLED = False
MAX_RUN_HTML_CACHE_ENTRIES = 800
MAX_RUN_HTML_BYTES = 2 * 1024 * 1024


def session() -> requests.Session:
    """Session HTTP propre au thread courant."""
    sess = getattr(_SESSION_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        _SESSION_LOCAL.session = sess
    return sess


def _run_cached_html(url: str) -> str | None:
    if not _RUN_CACHE_ENABLED:
        return None
    with _RUN_CACHE_LOCK:
        return _RUN_HTML_CACHE.get(canonical_url(url))


def _remember_run_html(url: str, html: str):
    if not _RUN_CACHE_ENABLED or not html or len(html) > MAX_RUN_HTML_BYTES:
        return
    with _RUN_CACHE_LOCK:
        if len(_RUN_HTML_CACHE) < MAX_RUN_HTML_CACHE_ENTRIES:
            _RUN_HTML_CACHE.setdefault(canonical_url(url), html)


@contextmanager
def shared_run_cache():
    """Mutualise les mêmes pages entre profils sans persistance inter-exécutions."""
    global _RUN_CACHE_ENABLED
    with _RUN_CACHE_LOCK:
        _RUN_HTML_CACHE.clear()
        _RUN_DEAD_URL_CACHE.clear()
        _RUN_CACHE_ENABLED = True
    try:
        yield
    finally:
        with _RUN_CACHE_LOCK:
            _RUN_CACHE_ENABLED = False
            _RUN_HTML_CACHE.clear()
            _RUN_DEAD_URL_CACHE.clear()


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _atomic_write_text(path: Path, text: str, keep_backup: bool = False):
    """Écrit un fichier d'un seul coup, sans laisser de contenu tronqué.

    Le fichier temporaire est créé dans le même répertoire afin que os.replace()
    reste atomique. Les fichiers d'état conservent en plus une copie précédente
    lisible si une donnée logiquement corrompue devait être écrite.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if keep_backup and path.exists():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            else:
                backup_temp = path.with_name(f".{path.name}.bak.tmp")
                try:
                    shutil.copy2(path, backup_temp)
                    os.replace(backup_temp, _backup_path(path))
                finally:
                    backup_temp.unlink(missing_ok=True)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value):
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        keep_backup=True,
    )


def _load_json_file(path: Path, default):
    """Charge un JSON et se rabat sur la dernière copie valide si nécessaire."""
    for candidate in (path, _backup_path(path)):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return default

# ---------------------------------------------------------------------------
# Mots-clés et zones — ÉLARGIS (point 3)
# ---------------------------------------------------------------------------

KEYWORDS = [
    # --- Enseignement (gymnase / post-obligatoire — PAS de FLE) ---
    "professeur français", "professeur de français", "enseignant français",
    "enseignant de français", "maître de français", "maîtresse de français",
    "français cdd", "expression orale", "diction", "culture générale",
    "maturité", "gymnase", "post-obligatoire", "école de commerce",
    "remplacement français", "suppléance", "chargé d'enseignement",
    "maître d'enseignement général / français",
    "maître d'enseignement général - français",
    "maître d'enseignement général – français",
    "maîtresse d'enseignement général / français",
    "maîtresse d'enseignement général - français",
    "maîtresse d'enseignement général – français",
    # --- Rédaction / édition ---
    "rédacteur", "rédactrice", "rédacteur web", "concepteur-rédacteur",
    "secrétaire de rédaction", "relecteur", "relectrice",
    "correcteur", "correctrice", "lecteur-correcteur", "rewriter",
    "éditeur", "éditrice", "assistant d'édition", "chargé d'édition",
    "maison d'édition", "content manager", "content editor",
    "iconographe", "concepteur de contenu",
    # --- Bibliothèque / documentation / archives ---
    "libraire", "bibliothécaire", "documentaliste", "archiviste",
    "aide-bibliothécaire", "agent en information documentaire",
    "spécialiste en information documentaire",
    # --- Journalisme / communication ---
    "journaliste", "journaliste rédacteur", "chargé de communication",
    "chargée de communication", "attaché de presse", "attachée de presse",
    "responsable de communication", "community manager",
    "chargé de contenu éditorial",
    # --- Traduction ---
    # NB : « réviseur » nu retiré (= réviseur comptable/audit) ; on garde la
    # forme métier « traducteur-réviseur ».
    "traducteur", "traductrice", "traducteur-réviseur",
    # --- Culture / médiation / patrimoine ---
    "médiateur culturel", "médiatrice culturelle", "médiation culturelle",
    "chargé de projet culturel", "chargée de projet culturel",
    "chargé de production", "administrateur culturel",
    "animateur culturel", "animatrice culturelle",
    "muséologue", "commissaire d'exposition", "régisseur d'œuvres",
    "chargé de collections", "responsable de collection",
    "chargé des publics", "chargée des publics", "responsable culturel",
    # --- Lettres / recherche / académique ---
    # NB : « collaborateur/collaboratrice scientifique » retiré des mots-clés
    # (trop générique) ; reste capté via le chemin « titre ambigu + description »
    # s'il y a un vrai contexte Lettres dans l'annonce.
    "lettres", "littérature", "assistant de recherche",
    "maître assistant", "assistant doctorant",
    "post-doctorant", "chargé de cours", "linguistique",
    # --- Ajouts recall (élargissement ciblé profil Lettres) ---
    # NB : « maître de discipline » / « maître spécialiste » volontairement EXCLUS
    # (trop génériques : captent les « disciplines spéciales » ACM/textile/sport,
    # hors Lettres). Les vrais postes restent pris par « français »/« expression
    # orale »/« diction », etc.
    "professeur de lettres", "enseignant de lettres",
    "conseiller pédagogique", "conseillère pédagogique",
    "répétiteur", "répétitrice", "écrivain public",
    "assistant éditorial", "assistante éditoriale", "coordinateur éditorial",
    "chef de projet éditorial", "rédacteur technique", "lexicographe",
    "terminologue", "médiathécaire", "responsable de médiathèque",
    "guide-conférencier", "guide conférencier", "chargé de médiation",
    "chargée de médiation", "médiation du livre", "animateur lecture",
    "spécialiste en information documentaire", "gestionnaire de l'information",
    # Intitulés anglais fréquents dans les organisations internationales.
    "content specialist", "editorial assistant", "editorial coordinator",
    "copywriter", "proofreader", "information specialist",
    "knowledge manager", "knowledge management officer",
    "communications officer", "publishing assistant", "library assistant",
]

# Termes désignant un poste d'enseignant (conventions cantonales variées)
TEACHING_TERMS = [
    "enseignement", "enseignant", "enseignante", "maître", "maîtresse",
    "professeur", "professeure", "chargé de cours", "chargée de cours",
    "formateur", "formatrice", "intervenant", "intervenante",
    "répétiteur", "répétitrice", "précepteur", "préceptrice",
]

# Matières / domaines liés aux Lettres Modernes
LETTRES_SUBJECTS = [
    "français", "lettres", "littérature", "expression orale", "diction",
    "culture générale", "linguistique",
    "édition", "rédaction", "communication", "médiation", "patrimoine",
    "lecture", "écriture", "humanités", "information documentaire", "livre",
]

EXCLUDE_KEYWORDS = [
    "informatique", "ingénieur", "développeur", "comptable", "médecin",
    "infirmier", "infirmière", "avocat", "électricien", "chauffeur",
    "technicien", "technicienne", "mécanicien", "soudeur", "plombier",
    "maçon", "cuisinier", "serveur", "vendeur automobile", "pépiniériste",
    "chef de culture", "viticole", "horticole",
    # Domaines scientifiques / techniques / labo (évite biologie, chimie…)
    "biologie", "biologiste", "chimie", "chimiste", "physique",
    "laboratoire", "labo", "culture cellulaire", "microbiologie",
    "biochimie", "pharma", "pharmaceutique", "analyste de laboratoire",
    "assistant technique", "assistante technique", "informaticien",
    "data scientist", "data analyst", "électronique", "robotique",
    "génie civil", "architecte", "géomètre", "dessinateur",
    # Santé / soins (hors Lettres)
    "soins", "soignant", "aide-soignant", "physiothérapeute",
    "ergothérapeute", "pharmacien", "dentiste", "vétérinaire",
    # Droit / juridique / sciences politiques
    "juriste", "juridique", "droit pénal", "droit civil", "sciences politiques",
    # Psychologie / orientation professionnelle
    "psychologue", "conseiller d'orientation", "conseillère d'orientation",
    # Économie / finance
    "économiste", "fiscalité", "audit", "auditeur", "réviseur d'entreprise",
    # Justice / greffe (capté à tort par « rédacteur »)
    "greffier", "greffière", "droit international", "professeur de droit",
    # Langues étrangères non francophones (assistants de langue hors profil)
    "chinois", "mandarin", "langue chinoise",
    # FLE / français langue étrangère (postes d'école de langues, hors profil).
    # « fle » (abréviation) est rejeté en mot entier — détecté dans le TITRE comme
    # dans la DESCRIPTION (cf. consider() qui lit la fiche pour les titres « français »).
    "français langue étrangère", "français langue seconde", "fle",
]

LETTRES_CONTEXTUAL_KEYWORDS = {
    "maturité", "maître assistant", "assistant de recherche",
    "assistant doctorant", "post-doctorant", "chargé de cours",
    # Trop large seul : il devient un signal de revue lorsqu'un contexte
    # publications/éditorial est visible dans le titre ou la description.
    "content specialist",
}
LETTRES_KEYWORDS = [kw for kw in KEYWORDS if kw not in LETTRES_CONTEXTUAL_KEYWORDS]
LETTRES_KEYWORDS.extend([
    "information documentaire", "assistant communication",
    "assistante communication", "communications manager", "communication manager",
    "social media coordinator", "editor", "editorial manager",
    "relations publiques", "public relations", "responsable éditorial",
    "responsable éditoriale", "coordinateur de publications",
    "coordinatrice de publications", "publications officer",
    "publication officer", "assistant linguistique", "assistante linguistique",
])
LETTRES_EXCLUDE_KEYWORDS = EXCLUDE_KEYWORDS[:]
LETTRES_SUBJECTS = LETTRES_SUBJECTS[:]

LETTRES_TITLE_EXCLUDE_KEYWORDS = [
    "assistant administratif", "assistante administrative", "commis administratif",
    "gestionnaire administratif", "assistant socio éducatif", "assistante socio éducative",
    "éducateur spécialisé", "éducatrice spécialisée", "enseignement spécialisé",
    "microtechniques", "technical studentship", "génie civil", "civil engineering",
    "primary english teacher", "english teacher", "teacher of english",
    "professeur d'anglais", "professeure d'anglais", "littérature anglaise",
    "enseignement général anglais",
    "enseignement général allemand", "enseignement général italien",
    "enseignement général mathématiques", "enseignement général musique",
    "enseignement général géographie", "enseignement général droit",
    "enseignement général philosophie", "éducation nutritionnelle",
    # Faux positifs fréquents des agrégateurs : contenu marketing/produit/vidéo
    # et records management générique, trop éloignés d'un profil Lettres.
    "creative content specialist", "training content specialist", "retail watchmaking",
    "watchmaking training", "video editor", "marketing video editor",
    "ai marketing", "records manager",
]

COMPTABILITE_KEYWORDS = [
    "comptable", "aide-comptable", "aide comptable",
    "assistant comptable", "assistante comptable",
    "collaborateur comptable", "collaboratrice comptable",
    "comptable junior", "comptable confirmé", "comptable confirmée",
    "spécialiste comptabilité", "specialiste comptabilite",
    "comptabilité", "comptabilite", "tenue de comptabilité",
    "tenue de comptabilite", "teneur de comptes", "tenue des comptes",
    "comptabilité fournisseurs", "comptabilite fournisseurs",
    "comptabilité débiteurs", "comptabilite debiteurs",
    "créanciers", "creanciers", "débiteurs", "debiteurs",
    "accounts payable", "accounts receivable", "ap accountant",
    "ar accountant", "accountant", "junior accountant", "bookkeeper",
    "finance assistant", "assistant finance", "assistante finance",
    "assistant financier", "assistante financière",
    "facturation", "billing", "payroll", "gestionnaire salaires",
    "gestionnaire de salaires", "salaires", "fiduciaire",
    "collaborateur fiduciaire", "collaboratrice fiduciaire",
    "réviseur comptable", "reviseur comptable", "audit comptable",
    "contrôleur de gestion", "controleur de gestion",
    "contrôle de gestion", "controle de gestion",
    "employé de commerce comptabilité", "employée de commerce comptabilité",
    "employe de commerce comptabilite", "employée de commerce fiduciaire",
    "gl accountant", "general ledger accountant", "financial accountant",
    "senior accountant", "accounting assistant", "accounting specialist",
    "accounting officer", "accounting", "financial controller",
    "finance officer", "accounts assistant", "directeur financier",
    "directrice financière", "directeur administratif et financier",
    "directrice administrative et financière", "finance director",
]

# Intitulés proches à examiner, mais trop ambigus pour entrer directement dans
# la sélection principale sans contexte comptable/fiduciaire supplémentaire.
COMPTABILITE_REVIEW_ONLY = [
    "responsable de mandats", "gestionnaire de mandats",
    "finance manager", "assistant family office", "assistante family office",
    "tax consultant", "senior tax consultant", "fiscaliste",
]

COMPTABILITE_EXCLUDE_KEYWORDS = [
    "enseignant", "enseignante", "professeur", "professeure",
    "maître de français", "maîtresse de français", "bibliothécaire",
    "documentaliste", "archiviste", "libraire", "rédacteur", "rédactrice",
    "journaliste", "traducteur", "traductrice", "médiateur culturel",
    "médiatrice culturelle", "muséologue", "commissaire d'exposition",
    "informatique", "développeur", "développeuse", "ingénieur", "ingénieure",
    "médecin", "infirmier", "infirmière", "aide-soignant", "aide-soignante",
    "psychologue", "éducateur", "éducatrice", "assistant social",
    "assistante sociale", "juriste", "avocat", "avocate", "greffier",
    "greffière", "technicien", "technicienne", "mécanicien", "électricien",
    "chauffeur", "cuisinier", "serveur", "vendeur automobile",
    "français langue étrangère", "français langue seconde", "fle",
    "software engineer", "backend software", "systems administrator",
    "system administrator", "account manager", "key account", "commercial",
    "contrôleur circulation aérienne", "night auditor", "hôtellerie",
    "assistant de direction", "assistante de direction", "executive assistant",
    "application consultant payroll", "payroll application consultant",
    "payroll project manager", "head of payroll", "responsable payroll",
]

COMPTABILITE_SUBJECTS = [
    "comptabilité", "comptabilite", "finance", "fiduciaire",
    "facturation", "salaires", "créanciers", "creanciers",
    "débiteurs", "debiteurs",
]

COMPTABILITE_SOURCE_TERMS = {
    "jobscout24": [
        "comptable", "aide-comptable", "assistant-comptable",
        "comptabilite", "fiduciaire", "finance", "facturation",
        "accounts-payable", "accounts-receivable", "accountant",
        "payroll", "controleur-de-gestion",
        "gl-accountant", "financial-accountant", "accounting-assistant",
        "responsable-de-mandats", "gestionnaire-de-mandats", "tax-consultant",
    ],
    "jobup": [
        "comptable", "aide-comptable", "assistant comptable",
        "comptabilité fournisseurs", "comptabilité débiteurs",
        "fiduciaire", "facturation", "assistant finance",
        "finance assistant", "payroll", "gestionnaire salaires",
        "contrôleur de gestion", "accountant",
        "GL accountant", "financial accountant", "accounting assistant",
        "responsable de mandats", "gestionnaire de mandats", "tax consultant",
    ],
    "adzuna": [
        "comptable", "aide-comptable", "assistant comptable",
        "fiduciaire", "facturation", "assistant finance",
        "accounts payable", "accounts receivable", "payroll",
        "contrôleur de gestion", "accountant",
        "GL accountant", "financial accountant", "accounting assistant",
        "responsable de mandats", "gestionnaire de mandats", "tax consultant",
    ],
    "jobs_ch": [
        "comptable", "aide-comptable", "assistant comptable",
        "comptabilité fournisseurs", "comptabilité débiteurs",
        "fiduciaire", "facturation", "assistant finance",
        "accounts payable", "accounts receivable", "payroll",
        "contrôleur de gestion", "accountant",
        "GL accountant", "financial accountant", "accounting assistant",
        "responsable de mandats", "gestionnaire de mandats", "tax consultant",
    ],
}

LETTRES_SOURCE_TERMS = {
    source: [
        "rédacteur", "éditeur", "bibliothécaire", "libraire", "correcteur",
        "traducteur", "journaliste", "documentaliste", "archiviste",
        "communication", "médiation culturelle", "médiateur culturel",
        "professeur français", "professeur de lettres", "assistant éditorial",
        "chargé de projet culturel", "chargé de médiation", "rédacteur technique",
        "editorial assistant", "copywriter",
        "proofreader", "information specialist", "knowledge manager",
        "communications officer", "publishing assistant", "library assistant",
        "relations publiques", "public relations", "responsable éditorial",
        "publications officer", "assistant linguistique",
    ]
    for source in ("jobscout24", "jobup", "adzuna", "jobs_ch")
}
LETTRES_SOURCE_TERMS["jobscout24"] = [
    term.replace(" ", "-") for term in LETTRES_SOURCE_TERMS["jobscout24"]
]

SYSTEMES_KEYWORDS = [
    # Ingénierie systèmes / infrastructure
    "ingénieur système", "ingénieure système", "ingénieur e système",
    "ingénieur infrastructure", "ingénieure infrastructure",
    "ingénieur e infrastructure",
    "system engineer", "systems engineer", "IT systems engineer",
    "infrastructure engineer", "cloud infrastructure engineer",
    "system and network engineer", "systems and network engineer",
    "spécialiste systèmes", "spécialiste infrastructure",
    "expert systèmes", "expert linux",
    "architecte systèmes", "architecte infrastructure",
    "responsable systèmes", "responsable infrastructure",
    # Administration et exploitation
    "administrateur système", "administratrice système",
    "administrateur trice système",
    "administration système", "administrateur infrastructure",
    "administratrice infrastructure", "administrateur linux",
    "administratrice linux", "administrateur trice linux",
    "system administrator",
    "systems administrator", "linux administrator", "linux engineer",
    "administrateur unix", "ingénieur unix",
    "ingénieur vmware", "administrateur vmware",
    "ICT system engineer",
    "sysadmin", "technicien systèmes", "technicienne systèmes",
    "technicien ne systèmes",
    "system technician", "system support engineer",
    "ingénieur exploitation", "ingénieure exploitation",
    "ingénieur e exploitation",
    "IT operations engineer", "systems operations engineer",
    "platform engineer", "site reliability engineer", "SRE engineer",
    "platform engineering", "cloud platform engineering", "server administrator",
    "Kubernetes tech lead",
    "infrastructure specialist", "IT infrastructure specialist",
    "spécialiste support windows", "support windows n2", "support windows n3",
    "windows support engineer", "endpoint engineer", "endpoint administrator",
    "Microsoft 365 administrator", "M365 administrator", "Intune administrator",
    "SCCM administrator", "cloud operations engineer", "cloud operations specialist",
    "DevOps engineer", "DevOps specialist",
    "Red Hat engineer", "RHEL engineer", "Ansible engineer",
    "Kubernetes administrator", "Kubernetes engineer",
    "OpenStack engineer", "virtualization engineer",
    "storage engineer", "backup engineer",
    # Les technologies systèmes seules restent un signal fort dans le titre.
    "linux", "openshift", "RHEL", "Red Hat", "Ansible", "OpenStack",
]

# Ces exclusions ne portent que sur le titre : une annonce systèmes peut citer
# des développeurs dans sa description sans devenir hors profil.
SYSTEMES_TITLE_EXCLUDE_KEYWORDS = [
    "développeur logiciel", "développeuse logiciel", "développeuse logicielle",
    "développeur web", "développeuse web", "développeur frontend",
    "développeuse frontend", "développeur backend", "développeuse backend",
    "développeur full stack", "développeuse full stack", "full stack developer",
    "frontend developer", "backend developer", "software developer",
    "software engineer", "ingénieur logiciel", "ingénieure logiciel",
    "développeur mobile", "développeuse mobile", "mobile developer",
    "data scientist", "data analyst", "business analyst",
    "software platform engineer", "data engineer",
    "développeur infrastructure", "développeuse infrastructure",
    "electrical engineer", "ingénieur électricien", "ingénieure électricienne",
    "electrical technician", "électricien", "électricienne", "électromécanicien",
    "electromechanical", "mechanical engineer", "mechanical technician",
    "cabling", "cooling", "ventilation", "refrigeration", "water treatment",
    "CVC", "biomédical", "biomedical", "industrialisation", "industrialization",
    "civil engineer", "génie civil", "physicist", "physicien",
    "assistant administratif", "assistante administrative", "commis administratif",
    "gestionnaire administratif", "trust administrator", "gestionnaire de trust",
    "ERP technical", "D365", "NetSuite", "payroll systems",
    "infrastructure télécom", "telecom infrastructure",
]

SYSTEMES_REVIEW_ONLY = [
    "data platform engineer", "embedded linux engineer",
    "embedded linux development engineer",
]

SYSTEMES_SOURCE_TERMS = {
    "jobscout24": [
        "ingenieur-systeme", "administrateur-systeme", "administrateur-linux",
        "linux", "system-engineer", "system-administrator", "sysadmin",
        "ingenieur-infrastructure", "infrastructure-engineer",
        "expert-systemes", "expert-linux", "administrateur-unix",
        "architecte-systemes", "vmware", "openshift", "ict-system-engineer",
        "platform-engineer", "site-reliability-engineer", "rhel", "red-hat",
        "ansible", "kubernetes", "openstack", "storage-engineer", "backup-engineer",
        "windows-support", "cloud-operations", "endpoint-engineer",
        "microsoft-365", "intune", "devops",
    ],
    "jobup": [
        "ingénieur système", "administrateur système", "administrateur linux",
        "linux engineer", "system engineer", "systems administrator", "sysadmin",
        "ingénieur infrastructure", "infrastructure engineer",
        "expert systèmes", "expert linux", "administrateur unix",
        "architecte systèmes", "vmware", "openshift", "ICT system engineer",
        "platform engineer", "site reliability engineer", "RHEL", "Red Hat",
        "Ansible", "Kubernetes", "OpenStack", "storage engineer", "backup engineer",
        "Windows support", "cloud operations", "endpoint engineer",
        "Microsoft 365", "Intune", "DevOps",
    ],
    "adzuna": [
        "ingénieur système", "administrateur système", "administrateur linux",
        "linux engineer", "system engineer", "systems administrator", "sysadmin",
        "ingénieur infrastructure", "infrastructure engineer",
        "expert systèmes", "expert linux", "administrateur unix",
        "architecte systèmes", "vmware", "openshift", "ICT system engineer",
        "platform engineer", "site reliability engineer", "RHEL", "Red Hat",
        "Ansible", "Kubernetes", "OpenStack", "storage engineer", "backup engineer",
        "Windows support", "cloud operations", "endpoint engineer",
        "Microsoft 365", "Intune", "DevOps",
    ],
    "jobs_ch": [
        "ingénieur système", "administrateur système", "administrateur linux",
        "linux engineer", "system engineer", "systems administrator", "sysadmin",
        "ingénieur infrastructure", "infrastructure engineer",
        "expert systèmes", "expert linux", "administrateur unix",
        "architecte systèmes", "vmware", "openshift", "ICT system engineer",
        "platform engineer", "site reliability engineer", "RHEL", "Red Hat",
        "Ansible", "Kubernetes", "OpenStack", "storage engineer", "backup engineer",
        "Windows support", "cloud operations", "endpoint engineer",
        "Microsoft 365", "Intune", "DevOps",
    ],
}

# Profil chargé à l'import ; l'exécution sans argument lance ensuite tous les profils.
DEFAULT_PROFILE = "lettres"
DEFAULT_RUN_PROFILE = "all"
SITE_BASE_URL = "https://gabigbarig.github.io/find_job/"
PROFILES = {
    "lettres": {
        "label": "Lettres modernes",
        "title": "Offres d'emploi – Lettres Modernes – Genève",
        "rss_title": "Offres Lettres Modernes – Genève",
        "description": "Veille d'offres Lettres modernes dans la zone Genève et Nyon proche",
        "keywords": LETTRES_KEYWORDS,
        "exclude_keywords": LETTRES_EXCLUDE_KEYWORDS,
        "title_exclude_keywords": LETTRES_TITLE_EXCLUDE_KEYWORDS,
        "subjects": LETTRES_SUBJECTS,
        "review_signals": [
            "editorial", "publishing", "proofreader", "library", "archives",
            "knowledge management", "information management",
            "communication manager", "communications manager", "social media coordinator",
            "humanities", "French teacher", "publications",
            "public relations", "relations publiques", "language assistant",
        ],
        "description_anchors": [
            "édition de contenu", "editorial content", "publication management",
            "correction de textes", "proofreading", "gestion documentaire",
            "information documentaire", "records management", "knowledge management",
            "médiation culturelle", "communication institutionnelle",
            "rédaction de contenu", "content creation", "littérature française",
        ],
        "min_score": 2,
    },
    "comptabilite": {
        "label": "Comptabilité",
        "title": "Offres d'emploi – Comptabilité – Genève",
        "rss_title": "Offres Comptabilité – Genève",
        "description": "Veille d'offres en comptabilité dans la zone Genève et Nyon proche",
        "keywords": COMPTABILITE_KEYWORDS,
        "exclude_keywords": COMPTABILITE_EXCLUDE_KEYWORDS,
        "review_only_title_keywords": COMPTABILITE_REVIEW_ONLY,
        "subjects": COMPTABILITE_SUBJECTS,
        "review_signals": [
            "accounting", "bookkeeping", "general ledger", "finance operations",
            "accounts payable", "accounts receivable", "facturation", "fiduciary",
            "responsable de mandats", "gestionnaire de mandats",
            "mandats fiduciaires", "finance manager", "family office",
            "tax consultant", "fiscaliste",
        ],
        "description_anchors": [
            "general ledger", "accounts payable", "accounts receivable",
            "financial statements", "monthly closing", "year-end closing",
            "écritures comptables", "clôture comptable", "bouclement comptable",
            "comptabilité fournisseurs", "comptabilité débiteurs", "tenue des comptes",
            "mandats fiduciaires", "déclarations fiscales", "fiscalité des personnes",
        ],
        "min_score": 2,
    },
    "systemes": {
        "label": "Systèmes & Linux",
        "title": "Offres d'emploi – Ingénierie systèmes & Linux – Genève",
        "rss_title": "Offres Systèmes & Linux – Genève",
        "description": (
            "Veille d'offres en ingénierie et administration systèmes, notamment "
            "Linux, dans la zone Genève et Nyon proche"
        ),
        "keywords": SYSTEMES_KEYWORDS,
        "exclude_keywords": [],
        "title_exclude_keywords": SYSTEMES_TITLE_EXCLUDE_KEYWORDS,
        "review_only_title_keywords": SYSTEMES_REVIEW_ONLY,
        "subjects": [],
        "review_signals": [
            "platform engineering", "cloud platform", "IT infrastructure",
            "IT operations", "site reliability", "system administration",
            "server administrator", "virtualization", "virtualisation",
            "storage engineer", "backup engineer", "container platform",
            "Red Hat", "Kubernetes", "OpenShift",
            "Windows N2", "Windows N3", "cloud operations", "endpoint management",
            "Microsoft 365", "M365", "Intune", "SCCM", "DevOps",
            "data platform", "embedded Linux",
        ],
        "description_anchors": [
            "Linux administration", "administration Linux", "Unix administration",
            "Windows Server", "Active Directory", "system administration",
            "infrastructure as code", "configuration management", "Ansible automation",
            "Kubernetes administration", "OpenShift administration", "VMware vSphere",
            "server infrastructure", "cloud infrastructure", "platform engineering",
            "endpoint management", "Microsoft 365 administration",
            "Intune administration", "SCCM administration", "cloud operations",
        ],
        "min_score": 2,
    },
}
ACTIVE_PROFILE = DEFAULT_PROFILE
ACTIVE_PROFILE_CONFIG = PROFILES[DEFAULT_PROFILE]
PROFILE_SOURCE_TERMS = {
    "lettres": LETTRES_SOURCE_TERMS,
    "comptabilite": COMPTABILITE_SOURCE_TERMS,
    "systemes": SYSTEMES_SOURCE_TERMS,
}
PROFILE_SOURCE_TERMS["lettres"].update({
    "reliefweb": [
        "communication", "communications", "editor", "editorial",
        "knowledge management", "information management", "records management",
        "publishing", "translation", "social media",
    ],
    "cagi": [
        "communication", "communications", "editorial", "knowledge management",
        "information management", "translation", "publications",
    ],
    "cinfo": [
        "communication", "communications", "editorial", "knowledge management",
        "information management", "translation", "publications",
    ],
})
PROFILE_SOURCE_TERMS["comptabilite"].update({
    "reliefweb": ["finance", "accounting", "accountant", "finance officer"],
    "cagi": ["finance", "accounting", "accountant"],
    "cinfo": ["finance", "accounting", "accountant"],
})
PROFILE_SOURCE_TERMS["systemes"].update({
    "reliefweb": ["IT", "systems", "infrastructure", "linux"],
    "cagi": ["IT", "systems", "infrastructure", "linux"],
    "cinfo": ["IT", "systems", "infrastructure", "linux"],
})


def source_terms(source: str, default_terms: list) -> list:
    """Termes de recherche adaptés au profil actif pour les agrégateurs privés."""
    return PROFILE_SOURCE_TERMS.get(ACTIVE_PROFILE, {}).get(source, default_terms)


# Communes du district de Nyon PROCHES de Genève (zone resserrée).
# On exclut volontairement Gland, Rolle, Lausanne, Morges (trop loin).
VAUD_ZONE = {
    "nyon", "coppet", "prangins", "mies", "tannay", "commugny",
    "founex", "bogis", "chavannes-de-bogis", "chavannes-des-bois",
    "borex", "eysins", "signy", "crassier", "grens", "duillier",
    "arnex-sur-nyon", "trélex", "givrins", "genolier",
}

GENEVE_ZONE = {
    "genève", "geneva", "genf", "carouge", "lancy", "meyrin", "vernier",
    "onex", "plan-les-ouates", "thônex", "bernex", "chêne-bougeries",
    "chêne-bourg", "pregny-chambésy", "grand-saconnex", "saconnex",
    "satigny", "dardagny", "russin", "avully", "avusy", "cartigny",
    "chancy", "laconnex", "soral", "gy", "jussy", "choulex", "cologny",
    "vandoeuvres", "puplinge", "presinge", "meinier",
    "collonge-bellerive", "hermance", "anières", "corsier", "céligny",
    "bellevue", "genthod", "versoix", "collex-bossy",
    # Quartiers et localités fréquemment utilisés seuls par les portails.
    "le lignon", "lignon", "châtelaine", "les avanchets", "avanchets",
    "cointrin", "acacias", "champel", "eaux-vives", "plainpalais",
    "servette", "petit-saconnex", "sécheron", "jonction", "vésenaz",
}

# Codes postaux suffisamment précis pour prouver l'appartenance à la zone. Les
# codes vaudois restent limités aux communes déjà admises, sans élargir à tout VD.
GENEVE_POSTCODES = set(range(1200, 1259)) | set(range(1290, 1299))
VAUD_TARGET_POSTCODES = {
    1260, 1262, 1263, 1266, 1270, 1271, 1272, 1274, 1277, 1279,
}
TARGET_POSTCODES = GENEVE_POSTCODES | VAUD_TARGET_POSTCODES

FOREIGN_ISO_CODES = {
    "us": "États-Unis", "fr": "France", "de": "Allemagne",
    "it": "Italie", "at": "Autriche", "es": "Espagne",
    "pt": "Portugal", "be": "Belgique", "nl": "Pays-Bas",
    "gb": "Royaume-Uni", "uk": "Royaume-Uni", "ie": "Irlande",
    "ca": "Canada", "ma": "Maroc", "dz": "Algérie", "tn": "Tunisie",
    "sn": "Sénégal", "cn": "Chine", "jp": "Japon", "sg": "Singapour",
    "in": "Inde", "au": "Australie", "br": "Brésil", "mx": "Mexique",
}

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
_SCRAPER_RUN_LOCAL = threading.local()


def log(msg: str):
    timestamp = local_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    # Verrou : évite l'entrelacement des lignes quand les scrapers tournent en parallèle.
    with _LOG_LOCK:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    diagnostics = getattr(_SCRAPER_RUN_LOCAL, "diagnostics", None)
    if diagnostics is not None:
        msg_norm = normalize(msg)
        disabled = any(marker in msg_norm for marker in (
            "source ignoree", "identifiants absents", "configuration absente",
        ))
        if disabled:
            _SCRAPER_RUN_LOCAL.status_hint = "disabled"
            diagnostics.append(str(msg)[:500])
        elif any(marker in msg_norm for marker in (
            "erreur", "echoue", "inaccessible", "invalide", "introuvable",
        )):
            diagnostics.append(str(msg)[:500])


def normalize(text: str) -> str:
    """Minuscule + suppression des accents pour un matching robuste."""
    text = str(text or "").replace("œ", "oe").replace("Œ", "OE")
    text = text.replace("æ", "ae").replace("Æ", "AE").lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


# --- Matching « mot entier » (corrige les faux positifs par sous-chaîne) ---
# Avant, `k in text` faisait matcher « sion » (Sion/Valais) dans « expression »,
# « labo » dans « collaboration », « bern » dans « bernex »… On compile chaque
# terme en regex à frontières de mot, tolérante à la typographie inclusive
# (rédacteur·trice, greffier-ère) et au pluriel léger.
_TERM_SEP = r"[\s\-·•/'’*.()]+"      # séparateurs internes/typographiques tolérés


def _compile_term(term: str, inflect: bool = True) -> "re.Pattern":
    """Compile un mot-clé en regex à frontières de mot (sur texte normalisé).

    inflect=True tolère un pluriel léger en -s (agent → agents). Désactivé pour
    les noms de lieux, qui ne s'accordent pas (sinon « berne » matcherait
    « bernex », commune genevoise).
    """
    words = normalize(term).split()
    body = _TERM_SEP.join(re.escape(w) for w in words)
    suffix = r"s?" if inflect else r""
    return re.compile(rf"\b{body}{suffix}\b")


def _compile_terms(terms, inflect: bool = True) -> list:
    return [_compile_term(t, inflect) for t in terms]


def term_in(text_norm: str, patterns: list) -> bool:
    """Vrai si l'un des motifs compilés apparaît (mot entier) dans le texte normalisé."""
    return any(p.search(text_norm) for p in patterns)


_KW_RE = _compile_terms(KEYWORDS)
_EXCLUDE_RE = _compile_terms(EXCLUDE_KEYWORDS)
_TITLE_EXCLUDE_RE = []
_REVIEW_ONLY_RE = []
_TEACHING_RE = _compile_terms(TEACHING_TERMS)
_SUBJECTS_RE = _compile_terms(LETTRES_SUBJECTS)
_REVIEW_RE = _compile_terms(PROFILES[DEFAULT_PROFILE].get("review_signals", []))
_DESC_ANCHOR_RE = _compile_terms(PROFILES[DEFAULT_PROFILE].get("description_anchors", []))


def profile_url(profile: str) -> str:
    return f"{SITE_BASE_URL}{profile}/"


def configure_profile(profile: str):
    """Active les critères, chemins et libellés d'un profil de veille."""
    global ACTIVE_PROFILE, ACTIVE_PROFILE_CONFIG
    global KEYWORDS, EXCLUDE_KEYWORDS, MIN_SCORE
    global DATA_DIR, DOCS_DIR, SEEN_FILE, RESULTS_FILE, PUBLIC_FILE
    global LOG_FILE, HEALTH_FILE, RSS_FILE, REVIEW_FILE, REJECTIONS_FILE, COVERAGE_FILE
    global DETAIL_CACHE_FILE
    global _KW_RE, _EXCLUDE_RE, _TITLE_EXCLUDE_RE, _REVIEW_ONLY_RE
    global _SUBJECTS_RE, _REVIEW_RE
    global _DESC_ANCHOR_RE

    if profile not in PROFILES:
        raise ValueError(f"Profil inconnu : {profile}")

    cfg = PROFILES[profile]
    ACTIVE_PROFILE = profile
    ACTIVE_PROFILE_CONFIG = cfg
    KEYWORDS = list(cfg["keywords"])
    EXCLUDE_KEYWORDS = list(cfg["exclude_keywords"])
    MIN_SCORE = int(os.environ.get("MIN_SCORE", str(cfg.get("min_score", 2))))
    _KW_RE = _compile_terms(KEYWORDS)
    _EXCLUDE_RE = _compile_terms(EXCLUDE_KEYWORDS)
    _TITLE_EXCLUDE_RE = _compile_terms(cfg.get("title_exclude_keywords", []))
    _REVIEW_ONLY_RE = _compile_terms(cfg.get("review_only_title_keywords", []))
    _SUBJECTS_RE = _compile_terms(cfg.get("subjects", []))
    _REVIEW_RE = _compile_terms(cfg.get("review_signals", []))
    _DESC_ANCHOR_RE = _compile_terms(cfg.get("description_anchors", []))

    DATA_DIR = DATA_ROOT / profile
    DOCS_DIR = DOCS_ROOT / profile
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE = DATA_DIR / "seen_jobs.json"
    RESULTS_FILE = DATA_DIR / "results.html"
    PUBLIC_FILE = DOCS_DIR / "index.html"
    LOG_FILE = DATA_DIR / "scraper.log"
    HEALTH_FILE = DATA_DIR / "health.json"
    RSS_FILE = DOCS_DIR / "feed.xml"
    REVIEW_FILE = DATA_DIR / "review_jobs.json"
    REJECTIONS_FILE = DATA_DIR / "rejections.json"
    COVERAGE_FILE = DATA_DIR / "query_coverage.json"
    DETAIL_CACHE_FILE = DATA_DIR / "detail_cache.json"


def bootstrap_legacy_profile_data():
    """Copie l'ancien historique racine vers le profil Lettres au premier lancement."""
    if ACTIVE_PROFILE != "lettres":
        return
    legacy_names = ("seen_jobs.json", "all_jobs.json", "health.json")
    if any((DATA_DIR / name).exists() for name in legacy_names):
        return
    for name in legacy_names:
        src = DATA_ROOT / name
        dst = DATA_DIR / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

# Marqueurs de titre générique : déclenchent la lecture de la description.
_AMBIGUOUS_MARKERS = [
    "collaborateur", "collaboratrice", "assistant", "assistante",
    "charge de mission", "chargee de mission", "charge de projet",
    "chargee de projet", "specialiste", "responsable", "adjoint",
    "coordinateur", "coordinatrice", "gestionnaire", "conseiller",
    "conseillere", "agent", "stagiaire",
    "officer", "specialist", "coordinator", "associate", "manager",
    "consultant", "analyst", "technician", "administrator",
]
_AMBIGUOUS_RE = _compile_terms(_AMBIGUOUS_MARKERS)

# Titres « à risque FLE » : un poste touchant au français / aux langues peut
# cacher du FLE (école de langues, hors profil) sans que le titre le dise. Pour
# ceux-là on lit la fiche pour repérer un FLE caché dans la description.
_FLE_RISK_RE = _compile_terms(["francais", "langue", "linguistique", "alphabetisation"])

# Termes FLE proprement dits — exclusion CIBLÉE (détectée dans le titre OU la
# description). Volontairement restreinte : on ne rejette que sur un vrai signal
# FLE, pas sur le bruit d'une page (autres annonces, menus de catégories).
_FLE_RE = _compile_terms([
    "fle", "français langue étrangère", "français langue seconde",
    "français langue d'intégration", "français langue d'accueil",
])


def fle_risk(title: str) -> bool:
    """Vrai si le titre justifie de lire la fiche pour exclure un FLE caché."""
    return term_in(normalize(title), _FLE_RISK_RE)


def is_fle(title: str, description: str = "") -> bool:
    """Vrai si un terme FLE figure dans le titre ou la description."""
    return term_in(normalize(title + " " + description), _FLE_RE)

# Détection de langue — normalize() enlève les accents, donc « für » devient « fur ».
# Les titres de métiers sont souvent en anglais même quand toute la fiche est en
# allemand. On combine donc des marqueurs métier forts et des mots fonctionnels,
# avec des contre-signaux français pour conserver les annonces bilingues.
_DE_STRONG = {
    "pflegefachfrau", "pflegefachmann", "pflegefachperson",
    "nachtwache", "ausbildung", "verantwortung", "bewerber",
    "stellenanzeige", "fachverantwortung", "privatstation", "arbeitszeit",
    "dienstleistung", "anforderungen",
    "datenbank", "uberwachung", "teamleiter", "infrastruktur",
    "optimierung", "plattformen", "befristet", "stellenantritt", "arbeitspensum",
    "aufgabenbereich", "berufserfahrung", "bewerbung", "deutschkenntnisse",
    "fachkenntnisse", "geschaftsleitung", "kenntnisse", "mitarbeiter",
    "sachbearbeiter", "tatigkeit", "voraussetzungen", "weiterentwicklung",
}
_DE_COMMON = {
    "und", "fur", "oder", "nach", "beim", "mit", "als", "der", "die",
    "das", "den", "dem", "von", "zur", "zum", "auf", "aus",
    "ein", "eine", "einer", "einen", "einem", "im", "am", "bei", "wir",
    "sie", "ihre", "ihren", "ihrer", "ihrem", "ihr", "du", "dein",
    "deine", "dich", "unser", "unsere", "sich", "auch", "nicht", "sowie",
    "sind", "ist", "haben", "wird", "werden", "bieten", "bringen",
    "arbeit", "aufgaben", "stelle",
}
_FR_COMMON = {
    "afin", "ainsi", "avec", "aux", "candidature", "ce", "ces", "cette",
    "competences", "dans", "dont", "equipe", "etre", "francais", "missions",
    "notre", "nous", "offrons", "pour", "poste", "profil", "que", "qui",
    "recherche", "recherchons", "responsabilites", "travail", "une", "votre",
    "vos", "vous",
}


def load_seen() -> set:
    value = _load_json_file(SEEN_FILE, [])
    return set(value) if isinstance(value, list) else set()


def save_seen(seen: set):
    _atomic_write_json(SEEN_FILE, sorted(seen))


def job_id(title: str, url: str) -> str:
    raw = canonical_url(url) or title.lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()


def legacy_job_id(title: str, url: str) -> str:
    """Identité utilisée avant le nettoyage des paramètres, pour migrer le suivi."""
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = re.sub(r"/+$", "", parsed.path) or "/"
        raw = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        ))
    raw = raw or title.lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()


def canonical_url(url: str) -> str:
    """URL stable, sans suivi ; conserve les fragments qui identifient une offre."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = re.sub(r"/+$", "", parsed.path) or "/"
    tracking_keys = {
        "fbclid", "gclid", "mc_cid", "mc_eid", "referrer", "sourceid",
        "trackingid", "trk", "trkemail", "ghsrc", "campaign",
    }
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_norm = normalize(key).replace("-", "").replace("_", "")
        if key_norm.startswith("utm") or key_norm in tracking_keys:
            continue
        query.append((key, value))
    query.sort(key=lambda item: (item[0].lower(), item[1]))
    # Certains portails SPA (notamment l'État de Vaud) mettent l'identifiant
    # d'offre uniquement après « # ». Le supprimer fusionnerait toutes les fiches.
    fragment = parsed.fragment if re.search(
        r"(?:^|/)(?:job|jobs|offre|posting)/", parsed.fragment, re.I
    ) else ""
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",
        urlencode(query, doseq=True),
        fragment,
    ))


def posting_identity(job: dict) -> str:
    """Identifiant ATS stable, séparé par portail pour éviter les collisions."""
    external_id = str(
        job.get("external_id") or job.get("posting_id") or ""
    ).strip()
    parsed = urlparse(job.get("url", ""))
    if not external_id:
        query = {key.lower(): value for key, value in parse_qsl(parsed.query)}
        for key in ("jobid", "job_id", "postingid", "requisitionid", "reqid"):
            if query.get(key):
                external_id = query[key]
                break
    if not external_id:
        return ""
    host = normalize(parsed.netloc)
    namespace = normalize(job.get("source", "") or job_employer(job))
    owner = f"{host}|{namespace}" if host else namespace
    return f"{owner}|{normalize(external_id)}"


def tracking_id(job: dict) -> str:
    """Identité de suivi stable même si une URL de diffusion change."""
    stable_posting = posting_identity(job)
    if stable_posting:
        raw = f"posting|{stable_posting}"
    else:
        employer = normalize(job_employer(job))
        location = normalize(display_location(job.get("location", "")))
        title = title_fingerprint(job.get("title", ""))
        raw = f"content|{employer}|{title}|{location}"
        if not employer:
            raw += f"|{urlparse(canonical_url(job.get('url', ''))).netloc}"
    return hashlib.md5(raw.encode()).hexdigest()


def relevance_score(title: str, description: str = "") -> int:
    """Nombre de mots-clés distincts trouvés (titre compte double).

    Sert à trier les offres : un score élevé = forte correspondance.
    """
    t_norm = normalize(title)
    d_norm = normalize(description)
    score = 0
    for kw in _KW_RE:
        if kw.search(t_norm):
            score += 2          # présence dans le titre = signal fort
        elif kw.search(d_norm):
            score += 1
    # Combinaison « enseignement + matière Lettres » : signal fort équivalent à un
    # mot-clé. Indispensable pour rester COHÉRENT avec is_relevant() — sinon une
    # offre acceptée par cette règle (ex. « Enseignant français ») garde un score 0
    # et serait recalée par le seuil MIN_SCORE de passes_filters().
    if term_in(t_norm, _TEACHING_RE) and term_in(t_norm, _SUBJECTS_RE):
        score += 2
    elif (term_in(t_norm + " " + d_norm, _TEACHING_RE)
          and term_in(t_norm + " " + d_norm, _SUBJECTS_RE)):
        score += 1
    return score


def is_relevant(title: str, description: str = "") -> bool:
    title_norm = normalize(title)
    description_norm = normalize(description)
    text = f"{title_norm} {description_norm}"
    # Un intitulé métier précis reste pertinent même si la description mentionne
    # un autre département (source fréquente de faux négatifs).
    if term_in(title_norm, _TITLE_EXCLUDE_RE) or term_in(title_norm, _EXCLUDE_RE):
        return False
    if term_in(title_norm, _KW_RE):
        return True
    if term_in(description_norm, _EXCLUDE_RE):
        return False
    if term_in(description_norm, _KW_RE):
        return True
    if term_in(text, _TEACHING_RE) and term_in(text, _SUBJECTS_RE):
        return True
    return False


def weak_relevance_reasons(title: str, description: str = "") -> list:
    """Signaux contrôlés réservés à « À vérifier ».

    Un signal dans le titre suffit. Une description seule doit contenir au moins
    deux expressions métier fortes. Aucun rapprochement orthographique générique
    n'est autorisé : il confondait notamment direction/diction et technician/
    technicien.
    """
    title_norm = normalize(title)
    description_norm = normalize(description[:2500])
    title_reasons = []
    for signal, pattern in zip(ACTIVE_PROFILE_CONFIG.get("review_signals", []), _REVIEW_RE):
        if pattern.search(title_norm):
            title_reasons.append(f"titre : {signal}")
    if title_reasons:
        return title_reasons[:4]
    description_reasons = [
        anchor for anchor, pattern in zip(
            ACTIVE_PROFILE_CONFIG.get("description_anchors", []), _DESC_ANCHOR_RE
        ) if pattern.search(description_norm)
    ]
    if len(description_reasons) >= 2:
        return [f"description : {anchor}" for anchor in description_reasons[:4]]
    return []


def strict_title_match(title: str) -> bool:
    """La sélection principale exige un signal métier explicite dans le titre."""
    title_norm = normalize(title)
    if term_in(title_norm, _TITLE_EXCLUDE_RE) or term_in(title_norm, _EXCLUDE_RE):
        return False
    if term_in(title_norm, _REVIEW_ONLY_RE):
        return False
    if term_in(title_norm, _KW_RE):
        return True
    if (ACTIVE_PROFILE == "lettres" and term_in(title_norm, _TEACHING_RE)
            and term_in(title_norm, _SUBJECTS_RE)):
        return True
    return False


def _language_signal_counts(text: str) -> tuple[int, int, int]:
    """Compte les marqueurs allemands forts/usuels et les marqueurs français."""
    words = re.findall(r"\b[^\W\d_]+\b", normalize(text), flags=re.UNICODE)
    de_strong = sum(word in _DE_STRONG for word in words)
    de_common = sum(word in _DE_COMMON for word in words)
    fr_common = sum(word in _FR_COMMON for word in words)
    return de_strong, de_common, fr_common


def is_french_text(title: str, description: str = "") -> bool:
    """Retourne False si le titre ou la fiche est clairement en allemand.

    Un titre anglais seul reste indéterminé. Une vraie version française ou
    bilingue est conservée ; une description dominée par l'allemand est rejetée.
    """
    title_strong, title_de, title_fr = _language_signal_counts(title)
    if title_strong and not title_fr:
        return False
    if title_de >= 2 and title_de > title_fr:
        return False
    if description:
        desc_strong, desc_de, desc_fr = _language_signal_counts(description[:5000])
        clearly_german = (
            desc_de >= 6
            and desc_de >= 2 * max(desc_fr, 1)
            and (desc_strong >= 1 or desc_de >= 10)
        )
        if clearly_german:
            return False
    return True


def title_is_ambiguous(title: str) -> bool:
    """Vrai si le titre ne matche pas seul mais mérite qu'on lise la description.

    Cas typique : titres génériques (« collaborateur », « assistant »,
    « chargé de mission ») qui peuvent cacher un poste Lettres.
    """
    t = normalize(title)
    if term_in(t, _TITLE_EXCLUDE_RE) or term_in(t, _EXCLUDE_RE):
        return False                       # exclu d'office, inutile d'aller plus loin
    if term_in(t, _KW_RE):
        return False                       # déjà pertinent, pas besoin du détail
    return term_in(t, _AMBIGUOUS_RE)


def expire_old_jobs(all_jobs: list, seen: set) -> tuple:
    now = local_now()
    cutoff = now - timedelta(days=EXPIRY_DAYS)
    fresh, expired_ids = [], set()
    for j in all_jobs:
        try:
            found_at = parse_local_datetime(j["found_at"])
        except (KeyError, TypeError, ValueError):
            fresh.append(j)
            continue
        valid_through = j.get("valid_through", "")
        expired_by_source = False
        if valid_through:
            try:
                expired_by_source = parse_local_datetime(valid_through).date() < now.date()
            except (TypeError, ValueError):
                pass
        if found_at >= cutoff and not expired_by_source:
            fresh.append(j)
        else:
            expired_ids.add(job_id(j["title"], j["url"]))
    removed = len(all_jobs) - len(fresh)
    if removed:
        log(
            f"Expiration : {removed} offre(s) retirée(s) "
            f"(ancienneté supérieure à {EXPIRY_DAYS} jours ou échéance dépassée)"
        )
    return fresh, seen - expired_ids


# --- Cache des parseurs robots.txt par domaine ---
_ROBOTS_CACHE: dict = {}
_ROBOTS_LOCK = threading.Lock()


def robots_allows(url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    from urllib.robotparser import RobotFileParser
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    with _ROBOTS_LOCK:
        rp = _ROBOTS_CACHE.get(base, "MISS")
    if rp == "MISS":
        # On récupère le robots.txt avec NOTRE User-Agent (session HTTP) : la lib
        # urllib.read() utilise l'UA « Python-urllib » que certains sites (educh.ch)
        # bloquent en 403, ce qui faisait conclure à tort « tout interdit ». On lit
        # donc exactement le robots.txt qui s'applique à nos requêtes réelles.
        rp = RobotFileParser()
        try:
            r = session().get(urljoin(base, "/robots.txt"), timeout=10)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None          # pas de robots.txt exploitable → pas de restriction
        except Exception:
            rp = None
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[base] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# --- Délai poli par domaine ---
# Verrou : avec les scrapers parallélisés (ThreadPoolExecutor dans main()), on
# garantit le délai poli PAR DOMAINE même si plusieurs threads visent le même hôte.
_LAST_REQUEST: dict = {}
_POLITE_LOCK = threading.Lock()


def _polite_wait(url: str):
    domain = urlparse(url).netloc
    with _POLITE_LOCK:
        last = _LAST_REQUEST.get(domain, 0.0)
        elapsed = time.time() - last
        wait = POLITE_DELAY - elapsed if elapsed < POLITE_DELAY else 0.0
        # On réserve le créneau avant de dormir pour qu'un autre thread visant le
        # même domaine s'aligne derrière (pas de rafale simultanée).
        _LAST_REQUEST[domain] = time.time() + wait
    if wait > 0:
        time.sleep(wait)


# Cache de résolution DNS par hôte (évite de re-tester un domaine mort à chaque URL).
_DNS_CACHE: dict = {}
_DNS_LOCK = threading.Lock()


def host_resolves(url: str) -> bool:
    """Vrai si le nom d'hôte de l'URL résout en DNS.

    Permet de sauter proprement un domaine hors-ligne (ex. job.educa.ch, NXDOMAIN)
    SANS tenter un fetch voué à l'échec qui polluerait les logs d'« Erreur fetch ».
    """
    host = urlparse(url).hostname
    if not host:
        return False
    with _DNS_LOCK:
        cached = _DNS_CACHE.get(host)
    if cached is not None:
        return cached
    try:
        socket.getaddrinfo(host, None)
        resolved = True
    except OSError:
        resolved = False
    with _DNS_LOCK:
        _DNS_CACHE[host] = resolved
    return resolved


def _is_permanent_error(exc: Exception) -> bool:
    """Vrai si l'erreur ne se résoudra jamais en réessayant (DNS, 404, 403).

    Inutile de boucler avec back-off sur un domaine qui n'existe pas (NXDOMAIN)
    ou une ressource interdite/absente : on logue une fois et on abandonne.
    """
    msg = str(exc)
    if "NameResolutionError" in msg or "Name or service not known" in msg:
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and resp.status_code in (403, 404):
        return True
    return False


# Empreintes d'une page d'erreur servie en HTTP 200 (ex. educh : erreur Smarty/PHP).
# On la traite comme un ÉCHEC de fetch : ça évite (a) de prendre une page cassée pour
# une « liste vide mais valide » (cf. canari de santé) et (b) d'empoisonner un futur
# cache de fiches. MARQUEURS À AJUSTER SI BESOIN.
_ERROR_PAGE_MARKERS = (
    "fatal error", "smarty_internal", "smartyexception", "undefined property",
    "uncaught exception", "stack trace", "internal server error", "service unavailable",
    "you have an error in your sql syntax",
)


def _looks_like_error_page(html: str) -> str:
    """Retourne la raison si `html` ressemble à une page d'erreur serveur (réponse
    HTTP 200 trompeuse), sinon "". Prudent : un marqueur d'erreur explicite, ou un
    corps minuscule sans le moindre lien (page quasi vide)."""
    if not html or not html.strip():
        return "corps vide"
    low = html.lower()
    for marker in _ERROR_PAGE_MARKERS:
        if marker in low:
            return f"marqueur « {marker} »"
    if len(html) < 600 and "<a" not in low:
        return f"corps minuscule ({len(html)} o, aucun lien)"
    return ""


def fetch(url: str, retries: int = 3):
    """GET poli avec respect de robots.txt, délai par domaine et back-off.

    Les erreurs permanentes (DNS mort, 403, 404) coupent court : pas de retry.
    Les vraies erreurs transitoires (timeout, 5xx) gardent les tentatives + back-off.
    """
    cached_html = _run_cached_html(url)
    if cached_html is not None:
        return BeautifulSoup(cached_html, "lxml")
    if not robots_allows(url):
        log(f"robots.txt interdit : {url} — ignoré")
        return None
    for attempt in range(retries):
        _polite_wait(url)
        try:
            r = session().get(url, timeout=15)
            r.raise_for_status()
            err = _looks_like_error_page(r.text)
            if err:
                log(f"⚠️  {url} : page d'erreur serveur ({err}) — traitée comme échec")
                return None
            _remember_run_html(url, r.text)
            return BeautifulSoup(r.text, "lxml")
        except Exception as e:
            if _is_permanent_error(e):
                log(f"Erreur fetch {url} (définitive, pas de retry): {e}")
                return None
            log(f"Erreur fetch {url} (tentative {attempt+1}): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def url_is_dead(url: str) -> bool:
    """Vrai si l'URL renvoie 404/410 (offre retirée par la source).

    TOLÉRANT par principe : tout autre cas (200, 3xx, 403, timeout, domaine
    injoignable, erreur réseau) est considéré « vivant » — on ne purge jamais une
    offre sur un simple aléa transitoire (mieux vaut un lien douteux qu'en perdre
    un valide). Sert à retirer de l'archive les liens devenus morts.
    """
    cache_key = canonical_url(url)
    if _RUN_CACHE_ENABLED:
        with _RUN_CACHE_LOCK:
            cached = _RUN_DEAD_URL_CACHE.get(cache_key)
        if cached is not None:
            return cached
    if not host_resolves(url):
        return False
    try:
        _polite_wait(url)
        r = session().head(url, timeout=10, allow_redirects=True)
        if r.status_code == 405:           # HEAD refusé : on retente en GET léger
            _polite_wait(url)
            r = session().get(url, timeout=15)
        dead = r.status_code in (404, 410)
        if _RUN_CACHE_ENABLED:
            with _RUN_CACHE_LOCK:
                _RUN_DEAD_URL_CACHE[cache_key] = dead
        return dead
    except Exception:
        return False


def dead_link_check_due(job: Job) -> bool:
    checked_at = job.get("url_checked_at", "")
    if not checked_at:
        return True
    try:
        age = local_now() - parse_local_datetime(checked_at)
    except (TypeError, ValueError):
        return True
    return age >= timedelta(hours=max(1, DEAD_LINK_CHECK_TTL_HOURS))


# --- Compteurs globaux de fetches de détail (garde-fous séparés) ---
_detail_fetch_count = 0      # lecture des titres ambigus (consider)
_employer_fetch_count = 0    # extraction de l'employeur (déduplication)
_COUNTERS_LOCK = threading.Lock()
_DETAIL_CACHE_LOCK = threading.Lock()
_detail_fields_cache: dict = {}
_DEFER_DETAIL_FETCHES = False
_pending_detail_candidates: list = []
_detail_source_yield: dict[str, float] = {}

# Canari d'extraction : nb de candidats BRUTS passés au funnel par source (clé =
# champ « source », ex. "educh.ch"). Remis à zéro au début de main(). Distingue
# « sélecteur cassé » (0 brut) de « 0 offre pertinente » (brut > 0, tout filtré).
_raw_counts: dict = {}
_query_counts: dict = {}
_rejection_counts: dict = {}
_rejection_samples: dict = {}
_rejection_by_source: dict = {}


def mark_raw_source(source: str):
    """Initialise le compteur brut d'une source, même si tous les candidats filtrent."""
    with _COUNTERS_LOCK:
        _raw_counts.setdefault(source, 0)


def record_raw_candidate(source: str):
    """Compte une carte/API extraite avant les filtres de métier et de lieu."""
    with _COUNTERS_LOCK:
        _raw_counts[source] = _raw_counts.get(source, 0) + 1


def mark_query(source: str, query: str):
    with _COUNTERS_LOCK:
        _query_counts.setdefault(f"{source}::{query}", 0)


def record_query_candidate(source: str, query: str):
    if not query:
        return
    with _COUNTERS_LOCK:
        key = f"{source}::{query}"
        _query_counts[key] = _query_counts.get(key, 0) + 1


def record_rejection(reason: str, job: dict):
    """Agrège les rejets sans saturer le journal quotidien."""
    reason = reason or "inconnu"
    source = job.get("source", "?")
    sample = {
        "title": clean_job_title(job.get("title", ""))[:140],
        "source": source,
        "location": job.get("location", ""),
        "url": job.get("url", ""),
    }
    with _COUNTERS_LOCK:
        _rejection_counts[reason] = _rejection_counts.get(reason, 0) + 1
        source_counts = _rejection_by_source.setdefault(source, {})
        source_counts[reason] = source_counts.get(reason, 0) + 1
        samples = _rejection_samples.setdefault(reason, [])
        if len(samples) < 5 and sample not in samples:
            samples.append(sample)


def save_rejection_report():
    report = {
        "updated_at": local_now().isoformat(),
        "profile": ACTIVE_PROFILE,
        "counts": dict(sorted(_rejection_counts.items())),
        "by_source": {
            source: dict(sorted(counts.items()))
            for source, counts in sorted(_rejection_by_source.items())
        },
        "samples": _rejection_samples,
    }
    _atomic_write_json(REJECTIONS_FILE, report)


def update_query_coverage() -> list:
    """Mémorise les requêtes muettes et alerte seulement après une vraie régression."""
    coverage = _load_json_file(COVERAGE_FILE, {})
    if not isinstance(coverage, dict):
        coverage = {}
    alerts = []
    for key, count in sorted(_query_counts.items()):
        entry = coverage.get(key, {"runs": 0, "max": 0, "zero_runs": 0})
        entry["runs"] += 1
        entry["last"] = count
        entry["max"] = max(entry.get("max", 0), count)
        entry["zero_runs"] = entry.get("zero_runs", 0) + 1 if count == 0 else 0
        entry["updated_at"] = local_now().isoformat()
        coverage[key] = entry
        if count == 0 and entry["max"] >= 3 and entry["zero_runs"] == 2:
            source, query = key.split("::", 1)
            alerts.append(
                f"🔎 {source} / « {query} » : aucun candidat brut depuis 2 recherches "
                f"(jusqu'à {entry['max']} auparavant)."
            )
    _atomic_write_json(COVERAGE_FILE, coverage)
    return alerts


_DESCRIPTION_NOISE_RE = re.compile(
    r"(?:\d+\s+emploi\(s\) similaire\(s\)|emplois? similaires?|"
    r"plus d['’]offres d['’]emploi|emplois fréquemment recherchés|"
    r"autres recherches d['’]emplois|related jobs|similar jobs|"
    r"à propos de l['’]entreprise|about the company|signaler cette offre|catégories\s*:)",
    re.IGNORECASE,
)


def sanitize_description(text: str, expected_title: str = "") -> str:
    """Retire recommandations, navigation et métadonnées des fiches d'emploi."""
    value = BeautifulSoup(str(text or ""), "lxml").get_text(" ", strip=True)
    value = re.sub(r"\bView more(?: responsibilities| skills)?\b", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    match = _DESCRIPTION_NOISE_RE.search(value)
    if match:
        prefix = value[:match.start()].strip()
        if match.start() < 500:
            title_norm = normalize(expected_title)
            if not title_norm or title_norm not in normalize(prefix):
                return ""
        value = prefix
    return value[:5000] if len(value) >= 40 else ""


def _json_ld_job_fields(soup: BeautifulSoup) -> dict:
    """Champs JobPosting structurés, préférables au texte visible bruité."""
    def find_jobposting(value):
        if isinstance(value, list):
            for item in value:
                found = find_jobposting(item)
                if found:
                    return found
        elif isinstance(value, dict):
            kind = value.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if any("JobPosting" in str(item) for item in kinds):
                return value
            for item in value.values():
                found = find_jobposting(item)
                if found:
                    return found
        return ""

    def first_text(value) -> str:
        if isinstance(value, list):
            return first_text(value[0]) if value else ""
        if isinstance(value, dict):
            for key in ("name", "text", "value"):
                if value.get(key):
                    return str(value[key]).strip()
            return ""
        return str(value or "").strip()

    def location_text(value) -> str:
        if isinstance(value, list):
            parts = [location_text(item) for item in value]
            return ", ".join(part for part in parts if part)
        if not isinstance(value, dict):
            return first_text(value)
        address = value.get("address")
        if isinstance(address, dict):
            parts = [
                first_text(address.get(key))
                for key in (
                    "streetAddress", "addressLocality", "addressRegion",
                    "postalCode", "addressCountry",
                )
            ]
            joined = ", ".join(part for part in parts if part)
            if joined:
                return joined
        return first_text(value)

    def joined_text(value) -> str:
        if isinstance(value, list):
            return ", ".join(filter(None, (first_text(item) for item in value)))
        return first_text(value)

    def identifier_text(value) -> str:
        if isinstance(value, dict):
            return str(value.get("value") or value.get("propertyID") or "").strip()
        return first_text(value)

    def salary_text(value) -> str:
        if not isinstance(value, dict):
            return first_text(value)

        def amount_text(amount) -> str:
            raw = first_text(amount)
            try:
                number = float(raw)
            except (TypeError, ValueError):
                return raw
            if number.is_integer():
                return f"{int(number):,}".replace(",", " ")
            return f"{number:g}".replace(".", ",")

        currency = first_text(value.get("currency"))
        amount = value.get("value", value)
        if isinstance(amount, dict):
            minimum = amount_text(amount.get("minValue"))
            maximum = amount_text(amount.get("maxValue"))
            unit = {
                "YEAR": "/an", "MONTH": "/mois", "WEEK": "/semaine",
                "DAY": "/jour", "HOUR": "/heure",
            }.get(first_text(amount.get("unitText")).upper(), "")
            numbers = "–".join(part for part in (minimum, maximum) if part)
            return " ".join(part for part in (numbers, currency) if part) + unit
        return " ".join(part for part in (amount_text(amount), currency) if part)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            posting = find_jobposting(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
        if posting:
            return {
                "description": str(posting.get("description", "") or ""),
                "location": location_text(posting.get("jobLocation")),
                "company": first_text(posting.get("hiringOrganization")),
                "date_posted": first_text(posting.get("datePosted")),
                "valid_through": first_text(posting.get("validThrough")),
                "employment_type": joined_text(posting.get("employmentType")),
                "job_location_type": joined_text(posting.get("jobLocationType")),
                "external_id": identifier_text(posting.get("identifier")),
                "salary": salary_text(posting.get("baseSalary")),
            }
    return {}


def _json_ld_job_description(soup: BeautifulSoup) -> str:
    """Description structurée schema.org, préférable au texte visible bruité."""
    return _json_ld_job_fields(soup).get("description", "")


_LOCATION_LABEL_RE = re.compile(
    r"\b(?:lieu de travail|localisation|location|work location|duty station|"
    r"job location|standort|arbeitsort)\s*[:\-]\s*([^|•\n\r]{2,140})",
    re.IGNORECASE,
)


def extract_location_hint(text: str) -> str:
    """Extrait un indice de lieu depuis une fiche, sans inventer de localisation."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    match = _LOCATION_LABEL_RE.search(value)
    if match:
        location = match.group(1).strip(" ,;:.")
        if location and len(location) <= 140:
            return location
    norm = normalize(value[:2500])
    if term_in(norm, _GEO_OK_RE):
        places = [place for place in sorted(GEO_OK, key=len, reverse=True)
                  if re.search(rf"\b{re.escape(normalize(place))}\b", norm)]
        if places:
            return places[0].title()
    return ""


def _page_fields(url: str, expected_title: str = "") -> dict:
    """Récupère le corps utile d'une fiche et quelques métadonnées stables."""
    soup = fetch(url, retries=2)
    if not soup:
        return {}
    structured = _json_ld_job_fields(soup)
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    main = (
        soup.select_one('[itemprop="description"], [data-cy="job-description"], '
                        '.job-description, #job-description')
        or soup.find("article") or soup.find("main") or soup.body or soup
    )
    visible_text = main.get_text(" ", strip=True)
    description = sanitize_description(
        structured.get("description", "") or visible_text, expected_title
    )
    location = structured.get("location", "") or extract_location_hint(visible_text)
    company = structured.get("company", "") or extract_employer(visible_text)
    return {
        **structured,
        "description": description,
        "location": location,
        "company": company,
    }


def _page_text(url: str, expected_title: str = "") -> str:
    """Récupère uniquement le corps utile d'une fiche d'emploi."""
    return _page_fields(url, expected_title).get("description", "")


def _load_detail_cache():
    global _detail_fields_cache
    raw = _load_json_file(DETAIL_CACHE_FILE, {})
    now = local_now()
    fresh = {}
    if isinstance(raw, dict):
        for url, entry in raw.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("fields"), dict):
                continue
            try:
                fetched_at = parse_local_datetime(entry.get("fetched_at", ""))
            except (TypeError, ValueError):
                continue
            if now - fetched_at <= timedelta(hours=max(1, DETAIL_CACHE_TTL_HOURS)):
                fresh[canonical_url(url)] = entry
    with _DETAIL_CACHE_LOCK:
        _detail_fields_cache = fresh


def _cached_detail_fields(url: str):
    key = canonical_url(url)
    with _DETAIL_CACHE_LOCK:
        entry = _detail_fields_cache.get(key)
    if not entry:
        return None
    try:
        fetched_at = parse_local_datetime(entry.get("fetched_at", ""))
    except (TypeError, ValueError):
        return None
    if local_now() - fetched_at > timedelta(hours=max(1, DETAIL_CACHE_TTL_HOURS)):
        with _DETAIL_CACHE_LOCK:
            _detail_fields_cache.pop(key, None)
        return None
    fields = entry.get("fields")
    return dict(fields) if isinstance(fields, dict) else None


def _cache_detail_fields(url: str, fields: dict):
    if not fields:
        return
    key = canonical_url(url)
    if not key:
        return
    with _DETAIL_CACHE_LOCK:
        _detail_fields_cache[key] = {
            "fetched_at": local_now().isoformat(),
            "fields": {
                name: str(fields.get(name, ""))
                for name in (
                    "description", "location", "company", "date_posted",
                    "valid_through", "employment_type", "job_location_type",
                    "external_id", "salary",
                )
                if fields.get(name)
            },
        }


def _save_detail_cache():
    with _DETAIL_CACHE_LOCK:
        entries = sorted(
            _detail_fields_cache.items(),
            key=lambda item: item[1].get("fetched_at", ""),
            reverse=True,
        )[:max(1, MAX_DETAIL_CACHE_ENTRIES)]
    _atomic_write_json(DETAIL_CACHE_FILE, dict(entries))


def fetch_detail_fields(url: str, expected_title: str = "") -> dict:
    """Description + lieu + employeur, dans le même quota que fetch_description()."""
    global _detail_fetch_count
    cached = _cached_detail_fields(url)
    if cached is not None:
        return cached
    with _COUNTERS_LOCK:
        if not FETCH_DESCRIPTIONS or _detail_fetch_count >= MAX_DETAIL_FETCHES:
            return {}
        _detail_fetch_count += 1
    fields = _page_fields(url, expected_title)
    _cache_detail_fields(url, fields)
    return fields


def fetch_description(url: str, expected_title: str = "") -> str:
    """Texte de détail pour lever l'ambiguïté d'un titre (point 1).

    Respecte MAX_DETAIL_FETCHES pour ne pas exploser le nombre de requêtes.
    Retourne "" si désactivé, quota atteint, ou échec.
    """
    return fetch_detail_fields(url, expected_title).get("description", "")


def fetch_employer_page(url: str) -> str:
    """Texte de détail pour extraire l'employeur d'une offre sans entreprise.

    Budget dédié (MAX_EMPLOYER_FETCHES), indépendant des fetches de titres
    ambigus, pour ne pas gonfler le volume de requêtes du scraping lui-même.
    """
    global _employer_fetch_count
    cached = _cached_detail_fields(url)
    if cached is not None:
        return cached.get("description", "")
    with _COUNTERS_LOCK:
        if _employer_fetch_count >= MAX_EMPLOYER_FETCHES:
            return ""
        _employer_fetch_count += 1
    fields = _page_fields(url)
    _cache_detail_fields(url, fields)
    return fields.get("description", "")


def extract_taux(text: str) -> str:
    """Extrait un taux d'activité (ex. '80%', '50-100%') depuis un texte."""
    m = re.search(r"(\d{1,3})\s*[-–à]\s*(\d{1,3})\s*%", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}%"
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        return f"{m.group(1)}%"
    return ""


def _warn_if_empty(source: str, jobs: list, expect_results: bool = True):
    if expect_results and not jobs and _pending_detail_count(source) == 0:
        log(f"⚠️  {source}: 0 offre — sélecteur potentiellement cassé ou source bloquée")


def dedup_by_url(jobs: list) -> list:
    seen_urls, unique = set(), []
    for j in jobs:
        url_key = canonical_url(j.get("url", ""))
        if url_key not in seen_urls:
            seen_urls.add(url_key)
            unique.append(j)
    return unique


def finalize(job: dict) -> dict:
    """Complète une offre : score de pertinence + taux d'activité.

    À appeler juste avant d'ajouter l'offre à la liste retournée.
    """
    job["title"] = clean_job_title(job.get("title", ""))
    desc = sanitize_description(job.get("description", ""), job["title"])
    job["description"] = desc
    job["score"] = relevance_score(job["title"], desc)
    if not job.get("taux"):
        job["taux"] = extract_taux(job["title"] + " " + desc)
    return job


def passes_filters(job: dict) -> bool:
    """Gate principal : signal métier dans le titre + zone géographique stricte.

    Appliqué à TOUTES les offres (scrapers + ré-validation de l'archive), pour
    une décision uniforme quel que soit le scraper d'origine.
    """
    title = job.get("title", "")
    description = job.get("description", "")
    if not is_french_text(title, description) or not strict_title_match(title):
        return False
    if relevance_score(title, description) < MIN_SCORE:
        return False
    # Un lieu explicite hors zone dans le titre, le champ lieu ou un
    # « Duty Station » structuré est bloquant, y compris pour l'archive.
    if job_has_far_location(job):
        return False
    if not job_in_zone(job):
        return False
    return True


# ---------------------------------------------------------------------------
# Identité de l'employeur (sert à la déduplication basée sur le contenu)
# ---------------------------------------------------------------------------

# Valeurs « bouche-trou » posées par les scrapers qui ignorent l'employeur.
EMPLOYER_PLACEHOLDERS = {
    "", "—", "-", "n/a", "educh.ch", "job-room", "jobup", "jobs.ch",
    "jobscout24", "indeed", "service clients", "service biel",
}

# Marqueur d'organisation/école (« Gymnase de Nyon », « Musée d'art et d'histoire »).
_EMPLOYER_MARKER = (
    r"Coll[èe]ge|Gymnase|[ÉE]cole|Lyc[ée]e|Cycle d'[Oo]rientation|"
    r"Universit[ée]|Haute [ÉE]cole|HEP|Institut|Fondation|Mus[ée]e|"
    r"Biblioth[èe]que|Centre|Association|D[ée]partement|Service"
)
# Un « jeton de nom » : mot capitalisé, construction avec apostrophe (« d'art »),
# ou mot de liaison (de, la, et…). On capture le marqueur + 1 à 5 jetons suivants.
_NAME_TOKEN = (
    r"(?:[A-ZÉÈÀÂÎÔ][\wÉÈÀÂÎÔéèàâîôûç’'\-]*"
    r"|[dlD][’'][a-zéèàâîôûç]+"
    r"|de|des|du|la|le|les|et|aux|au|à)"
)
_EMPLOYER_RE = re.compile(
    rf"\b({_EMPLOYER_MARKER})\s+({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,4}})",
    re.UNICODE,
)
# Jetons de liaison à rogner en fin de nom (« Université de » → « Université »).
_TRAILING_CONNECTORS = {"de", "des", "du", "la", "le", "les", "et", "aux", "au", "a"}


def is_meaningful_company(company: str) -> bool:
    """Vrai si `company` désigne un vrai employeur (pas un bouche-trou « — »)."""
    norm = normalize(company).strip()
    return len(norm) > 1 and norm not in EMPLOYER_PLACEHOLDERS


def extract_employer(text: str) -> str:
    """Extrait un nom d'organisation/école depuis un texte (best effort).

    Sert de SIGNAL DE DÉDUP : une extraction partielle (« Gymnase de Nyon »)
    suffit à distinguer deux postes au même intitulé. Retourne "" si rien.
    """
    m = _EMPLOYER_RE.search(text or "")
    if not m:
        return ""
    marker, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip(" ,;:.")
    # Rogne les mots de liaison résiduels en fin (« de Genève et » → « de Genève »).
    words = name.split()
    while words and normalize(words[-1]) in _TRAILING_CONNECTORS:
        words.pop()
    name = " ".join(words)
    # Garde-fou : un vrai nom propre contient une majuscule ou une apostrophe.
    # Sinon ce n'est que du remplissage (« Centre de ») → on ignore.
    if not name or not re.search(r"[A-ZÉÈÀÂÎÔ’']", name):
        return ""
    return f"{marker} {name}".strip()


def job_employer(job: dict) -> str:
    """Employeur de référence : `company` si réel, sinon l'école extraite."""
    company = job.get("company", "")
    if is_meaningful_company(company):
        return company
    employer = job.get("employer", "")
    return employer if is_meaningful_company(employer) else ""


_TITLE_AGE_PREFIX_RE = re.compile(
    r"^(?:aujourd['’]hui|hier|avant-hier|cette semaine|la semaine derni[eè]re|"
    r"le mois dernier|le trimestre dernier|il y a\s+\d+\s+"
    r"(?:heures?|jours?|semaines?|mois|trimestres?|ans?))\b"
    r"\s*(?:[·|:–—-]\s*)?",
    re.IGNORECASE,
)
_TITLE_GENDER_SUFFIX_RE = re.compile(
    r"\s*(?:\((?:[fhmdx][\/.-]?){2,}\)|(?:[\/|-]\s*)?[HFMWDX](?:[\/.-][HFMWDX])+)\s*$",
    re.IGNORECASE,
)
_TITLE_LOCATION_SUFFIX_RE = re.compile(
    r"\s*[-–—|]\s*(?:gen[eè]ve|geneva|genf|carouge|meyrin|vernier|onex|nyon)\s*$",
    re.IGNORECASE,
)


def clean_job_title(title: str) -> str:
    """Retire les marqueurs ajoutés par les plateformes, sans altérer le métier."""
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip()
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _TITLE_AGE_PREFIX_RE.sub("", cleaned).strip(" ·|:–—-")
    return cleaned or str(title or "").strip()


def display_location(location: str) -> str:
    """Francise les variantes usuelles de la zone sans modifier le filtrage."""
    value = re.sub(r"\s+", " ", str(location or "")).strip(" ,")
    if not value:
        return "—"
    value = re.sub(r"\bKanton Genf\b", "Canton de Genève", value, flags=re.I)
    value = re.sub(r"\bGenf\b", "Genève", value, flags=re.I)
    value = re.sub(r"\bGeneva\b", "Genève", value, flags=re.I)
    value = re.sub(r"\bGeneve\b", "Genève", value, flags=re.I)
    value = re.sub(r",?\s*(?:Schweiz|Switzerland|Suisse)\s*$", "", value, flags=re.I)
    return value.strip(" ,") or "Genève"


def matched_keywords(job: dict, limit: int = 8) -> list:
    """Mots-clés du profil expliquant le score d'une offre."""
    title_norm = normalize(job.get("title", ""))
    description_norm = normalize(job.get("description", ""))
    matches = []
    for keyword, pattern in zip(KEYWORDS, _KW_RE):
        if pattern.search(title_norm) or pattern.search(description_norm):
            label = keyword.strip()
            if normalize(label) not in {normalize(item) for item in matches}:
                matches.append(label)
        if len(matches) >= limit:
            break
    return matches


def job_contract(job: dict) -> str:
    """Extrait uniquement les types de contrat explicitement indiqués."""
    structured = str(job.get("employment_type", "") or "").strip()
    if structured:
        labels = {
            "full_time": "Temps plein", "fulltime": "Temps plein",
            "part_time": "Temps partiel", "parttime": "Temps partiel",
            "temporary": "Temporaire", "contractor": "Contrat",
            "intern": "Stage", "internship": "Stage",
        }
        key = normalize(structured).replace("-", "_").replace(" ", "_")
        return labels.get(key, structured)
    text = f"{job.get('title', '')} {job.get('description', '')}"
    labelled = re.search(
        r"(?:type de contrat|contrat)\s*[:\-]\s*(CDI|CDD|stage|temporaire|apprentissage)",
        text,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1).upper() if len(labelled.group(1)) <= 3 else labelled.group(1).title()
    title_match = re.search(r"\b(CDI|CDD|stage|temporaire|apprentissage)\b", job.get("title", ""), re.I)
    if title_match:
        value = title_match.group(1)
        return value.upper() if len(value) <= 3 else value.title()
    return ""


def job_deadline(job: dict) -> str:
    """Extrait une échéance quand la fiche contient un libellé non ambigu."""
    structured = str(job.get("valid_through", "") or "").strip()
    if structured:
        try:
            return parse_local_datetime(structured).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            pass
    text = job.get("description", "")
    match = re.search(
        r"(?:d[eé]lai d['’]inscription|date limite(?: de candidature)?)\s*:\s*"
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def job_posted_date(job: dict) -> str:
    """Formate la date de publication fournie par schema.org, si elle est valide."""
    structured = str(job.get("date_posted", "") or "").strip()
    if not structured:
        return ""
    try:
        return parse_local_datetime(structured).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return ""


def job_work_mode(job: dict) -> str:
    """Détecte un mode de travail seulement lorsqu'il est formulé explicitement."""
    if "telecommute" in normalize(job.get("job_location_type", "")):
        return "Télétravail"
    text = f"{job.get('title', '')} {job.get('description', '')[:1200]}"
    if re.search(r"(?:mode de travail|work mode)\s*[:\-]\s*hybride", text, re.I):
        return "Hybride"
    if re.search(r"(?:poste|travail)\s+(?:en\s+)?t[eé]l[eé]travail\b|\bremote position\b", text, re.I):
        return "Télétravail"
    return ""


def title_fingerprint(title: str) -> str:
    """Empreinte d'un titre, insensible à la typographie inclusive et à la ponctuation.

    « Greffier-ère », « Greffier·ère », « Greffier/ère » → même empreinte.
    Sert à fusionner les ré-publications d'une même offre.
    """
    stable = clean_job_title(title)
    # Les plateformes placent souvent le genre puis le taux à la fin, dans des
    # ordres différents : « F/H (100 %) » et « 100% H/F » restent la même offre.
    stable = re.sub(
        r"\b[FMHDWX]\s*(?:[/.\-]\s*[FMHDWX])+\b", " ", stable,
        flags=re.IGNORECASE,
    )
    stable = re.sub(r"\(?\s*\d{1,3}\s*%\s*\)?", " ", stable)
    stable = normalize(stable)
    stable = re.sub(r"\b\d+\s+vues?\b", " ", stable)
    stable = _TITLE_GENDER_SUFFIX_RE.sub("", stable)
    stable = _TITLE_LOCATION_SUFFIX_RE.sub("", stable)
    stable = re.sub(r"\bsystems\b", "system", stable)
    return re.sub(r"[^a-z0-9]+", "", stable)


_MULTILINGUAL_TITLE_CONCEPTS = {
    "consultant": _compile_terms(
        ["consultant", "consultante", "berater", "beraterin", "consulente"],
        inflect=False,
    ),
    "kubernetes": _compile_terms(["kubernetes"], inflect=False),
    "banking": _compile_terms(
        ["domaine bancaire", "bankwesen", "settore bancario"],
        inflect=False,
    ),
}


def multilingual_title_fingerprint(title: str) -> str:
    """Empreinte sémantique volontairement étroite pour traductions évidentes."""
    norm = normalize(clean_job_title(title))
    concepts = {
        concept for concept, patterns in _MULTILINGUAL_TITLE_CONCEPTS.items()
        if term_in(norm, patterns)
    }
    if "consultant" not in concepts or "kubernetes" not in concepts:
        return ""
    return "|".join(sorted(concepts))


def employer_fingerprint(value: str) -> str:
    """Normalise les formes juridiques sans confondre les noms eux-mêmes."""
    stable = normalize(value)
    stable = re.sub(
        r"\b(?:sa|ag|sarl|sàrl|gmbh|ltd|limited|inc|corp|corporation)\b",
        " ",
        stable,
    )
    return re.sub(r"[^a-z0-9]+", "", stable)


def is_duplicate(job: dict, fp_to_known: dict, seen_urls: set | None = None,
                 seen_postings: set | None = None) -> bool:
    """Vrai si `job` est un doublon d'une offre déjà retenue.

    Une URL d'offre identique est toujours un doublon, même si la source change
    le libellé de la carte (ex. compteur « 38 vues » chez educh.ch).

    Règle (choix utilisateur) : deux offres au même `title_fingerprint` sont des
    doublons, SAUF si toutes deux ont un employeur CONNU et DIFFÉRENT (cas « deux
    écoles distinctes au même intitulé », préservé via l'extraction d'employeur).

    `fp_to_known` mappe empreinte → set des employeurs connus déjà retenus.
    Met à jour `fp_to_known` au passage (enregistre l'offre conservée).
    """
    url_key = canonical_url(job.get("url", ""))
    if seen_urls is not None and url_key in seen_urls:
        return True
    fp = multilingual_title_fingerprint(job.get("title", ""))
    fp = f"multi:{fp}" if fp else title_fingerprint(job.get("title", ""))
    emp = employer_fingerprint(job_employer(job))
    posting_key = posting_identity(job)
    if seen_postings is not None and posting_key:
        if posting_key in seen_postings:
            return True
        # Un identifiant ATS distinct est une preuve plus forte qu'un titre
        # identique : deux réquisitions du même employeur doivent rester visibles.
        seen_postings.add(posting_key)
        if seen_urls is not None and url_key:
            seen_urls.add(url_key)
        known = fp_to_known.setdefault(fp, set())
        if emp:
            known.add(emp)
        return False
    known = fp_to_known.get(fp)
    if known is None:
        fp_to_known[fp] = {emp} if emp else set()
        if seen_urls is not None and url_key:
            seen_urls.add(url_key)
        if seen_postings is not None and posting_key:
            seen_postings.add(posting_key)
        return False                       # empreinte jamais vue → on garde
    if emp and emp not in known:
        known.add(emp)
        if seen_urls is not None and url_key:
            seen_urls.add(url_key)
        if seen_postings is not None and posting_key:
            seen_postings.add(posting_key)
        return False                       # employeur connu et distinct → on garde
    return True                            # même titre, pas de nouvel employeur → doublon


def deduplicate_jobs(jobs: list) -> list:
    """Retourne la meilleure variante de chaque annonce, toutes sources confondues."""
    ordered = sorted(
        jobs,
        key=lambda item: (0 if job_employer(item) else 1, item.get("found_at", "")),
    )
    fingerprints, urls, postings = {}, set(), set()
    return [
        item for item in ordered
        if not is_duplicate(item, fingerprints, urls, postings)
    ]


# Zone géographique acceptée : Genève + district de Nyon proche
GEO_OK = GENEVE_ZONE | VAUD_ZONE

# Lieux suisses explicitement trop loin → rejet immédiat (même si autres indices)
GEO_FAR = [
    "lausanne", "morges", "gland", "rolle", "yverdon", "vevey", "montreux",
    "fribourg", "neuchatel", "neuchâtel", "sion", "valais", "berne", "bern",
    "zurich", "zürich", "bale", "bâle", "basel", "lucerne", "luzern",
    "biel", "bienne", "delemont", "delémont", "jura", "aigle", "bulle",
    "pully", "renens", "vverdon", "winterthur", "saint-gall", "tessin",
    "lugano", "thoune", "coire", "chur", "schaffhouse", "zoug", "zug",
    "spreitenbach", "baden", "aarau", "argovie", "aargau", "soleure",
    "solothurn", "schwyz", "st-gall", "st. gall", "st. gallen",
    # Localités apparues dans les résultats nationaux malgré un filtre Genève.
    "urdorf", "schwerzenbach", "einsiedeln", "verbier",
]

# Pays et villes étrangères fréquents dans les portails internationaux. Ce
# registre n'essaie pas de géocoder le monde entier : il transforme seulement
# un lieu explicite en preuve hors zone. Un lieu réellement indéterminé reste
# éligible à « À vérifier ».
GEO_FOREIGN_COUNTRIES = [
    "france", "allemagne", "germany", "italie", "italy", "autriche", "austria",
    "espagne", "spain", "portugal", "belgique", "belgium", "pays-bas",
    "netherlands", "royaume-uni", "united kingdom", "angleterre", "england",
    "irlande", "ireland", "états-unis", "etats-unis", "united states", "usa",
    "canada", "maroc", "morocco", "algérie", "algerie", "algeria", "tunisie",
    "tunisia", "sénégal", "senegal", "chine", "china", "japon", "japan",
    "singapour", "singapore", "inde", "india", "australie", "australia",
    "brésil", "bresil", "brazil", "mexique", "mexico",
]
GEO_FOREIGN_CITIES = [
    "annemasse", "lyon", "paris", "lille", "grenoble", "chambéry", "chambery",
    "annecy", "ferney-voltaire", "saint-julien-en-genevois", "munich", "berlin",
    "francfort", "frankfurt", "milan", "rome", "bruxelles", "brussels",
    "londres", "london", "dublin", "madrid", "barcelone", "barcelona",
    "lisbonne", "lisbon", "new york", "washington", "montréal", "montreal",
    "toronto", "rabat", "casablanca", "tunis", "dakar", "shanghai", "pékin",
    "pekin", "beijing", "tokyo", "osaka", "delhi", "mumbai", "sydney",
    "schaan", "vaduz",
]
GEO_SWISS_CANTONS = {
    "aargau": "Argovie", "argovie": "Argovie",
    "bern": "Berne", "berne": "Berne",
    "zurich": "Zurich", "zürich": "Zurich",
    "basel": "Bâle", "bale": "Bâle", "bâle": "Bâle",
    "fribourg": "Fribourg", "valais": "Valais",
    "neuchatel": "Neuchâtel", "neuchâtel": "Neuchâtel",
    "jura": "Jura", "lucerne": "Lucerne", "luzern": "Lucerne",
    "solothurn": "Soleure", "soleure": "Soleure",
    "schwyz": "Schwyz", "tessin": "Tessin",
    "appenzell": "Appenzell", "glaris": "Glaris", "glarus": "Glaris",
    "grisons": "Grisons", "graubunden": "Grisons", "graubünden": "Grisons",
    "nidwald": "Nidwald", "nidwalden": "Nidwald",
    "obwald": "Obwald", "obwalden": "Obwald",
    "schaffhouse": "Schaffhouse", "schaffhausen": "Schaffhouse",
    "thurgovie": "Thurgovie", "thurgau": "Thurgovie",
    "uri": "Uri", "vaud": "Vaud", "waadt": "Vaud",
}
GEO_FAR_CITY_CANTONS = {
    "lausanne": "Vaud", "morges": "Vaud", "gland": "Vaud", "rolle": "Vaud",
    "yverdon": "Vaud", "vevey": "Vaud", "montreux": "Vaud", "aigle": "Vaud",
    "pully": "Vaud", "renens": "Vaud", "spreitenbach": "Argovie",
    "baden": "Argovie", "aarau": "Argovie", "lugano": "Tessin",
    "urdorf": "Zurich", "schwerzenbach": "Zurich",
    "einsiedeln": "Schwyz", "verbier": "Valais",
}

# Matching « mot entier » des lieux : « sion » (Valais) ne doit pas matcher
# « expreSSION », ni « bern » matcher « BERNex » (commune genevoise).
_GEO_FAR_RE = _compile_terms(GEO_FAR, inflect=False)
_GEO_OK_RE = _compile_terms(GEO_OK, inflect=False)
_GEO_FOREIGN_COUNTRY_RE = _compile_terms(GEO_FOREIGN_COUNTRIES, inflect=False)
_GEO_FOREIGN_CITY_RE = _compile_terms(GEO_FOREIGN_CITIES, inflect=False)
_GEO_OK_MATCHERS = [
    (place, _compile_term(place, inflect=False))
    for place in sorted(GEO_OK, key=len, reverse=True)
]
_GEO_FAR_MATCHERS = list(zip(GEO_FAR, _GEO_FAR_RE))
_GEO_FOREIGN_CITY_MATCHERS = list(zip(GEO_FOREIGN_CITIES, _GEO_FOREIGN_CITY_RE))
_GEO_FOREIGN_COUNTRY_MATCHERS = list(
    zip(GEO_FOREIGN_COUNTRIES, _GEO_FOREIGN_COUNTRY_RE)
)
_GEO_SWISS_CANTON_MATCHERS = [
    (alias, canton, _compile_term(alias, inflect=False))
    for alias, canton in GEO_SWISS_CANTONS.items()
]


def structured_geography(text: str) -> dict:
    """Classe un texte de localisation en zone cible, hors zone ou inconnu.

    Les champs exposés rendent la décision inspectable dans les tests et les
    diagnostics. Ils correspondent au niveau de preuve trouvé, sans prétendre
    déduire une adresse complète.
    """
    norm = normalize(text)
    postal_match = re.search(r"(?<!\d)([1-9]\d{3})(?!\d)", norm)
    postal_code = int(postal_match.group(1)) if postal_match else None
    target_postcode = postal_code if postal_code in TARGET_POSTCODES else None
    target_region = bool(re.search(
        r"(?:^|[,;/|\s])(?:ch\s*[-/]\s*ge|canton de geneve)"
        r"(?:$|[,;/|\s])",
        norm,
    ))
    # Les codes pays très courts ne sont interprétés que comme un champ final
    # délimité : « us » dans une phrase ne doit pas devenir une preuve de pays.
    iso_match = re.search(
        r"(?:^|[,;/|]\s*)(us|fr|de|it|at|es|pt|be|nl|gb|uk|ie|ca|ma|dz|tn|"
        r"sn|cn|jp|sg|in|au|br|mx)\s*$",
        norm,
    )
    foreign_iso = iso_match.group(1) if iso_match else ""
    explicit_swiss_postcode = bool(postal_code and re.search(
        rf"(?:ch\s*[- ]\s*{postal_code}|{postal_code}[^\n]{{0,80}}"
        r"(?:suisse|switzerland|schweiz))",
        norm,
    ))
    local_matches = [
        (match.start(), -len(place), place)
        for place, pattern in _GEO_OK_MATCHERS
        if (match := pattern.search(norm))
    ]
    city = min(local_matches)[2] if local_matches else ""
    far_city = next(
        (place for place, pattern in _GEO_FAR_MATCHERS if pattern.search(norm)),
        "",
    )
    foreign_city = next(
        (place for place, pattern in _GEO_FOREIGN_CITY_MATCHERS
         if pattern.search(norm)),
        "",
    )
    foreign_country = next(
        (country for country, pattern in _GEO_FOREIGN_COUNTRY_MATCHERS
         if pattern.search(norm)),
        "",
    )
    swiss_canton = next(
        (canton for _alias, canton, pattern in _GEO_SWISS_CANTON_MATCHERS
         if pattern.search(norm)),
        "",
    )
    if (far_city or foreign_city or foreign_country or foreign_iso
            or (explicit_swiss_postcode and not target_postcode)):
        evidence = (
            far_city or foreign_city or foreign_country or foreign_iso
            or str(postal_code)
        )
        return {
            "status": "outside", "postal_code": postal_code,
            "country": (
                foreign_country or FOREIGN_ISO_CODES.get(foreign_iso, "")
                or ("Suisse" if far_city or explicit_swiss_postcode else "")
            ),
            "canton": swiss_canton or GEO_FAR_CITY_CANTONS.get(far_city, ""),
            "city": far_city or foreign_city,
            "evidence": evidence,
        }
    if city or target_postcode or target_region:
        canton = (
            "Genève"
            if city in GENEVE_ZONE or target_region
            or target_postcode in GENEVE_POSTCODES
            else "Vaud"
        )
        return {
            "status": "target", "country": "Suisse", "canton": canton,
            "city": city, "postal_code": postal_code,
            "evidence": city or str(target_postcode or "CH-GE"),
        }
    return {
        "status": "unknown", "country": "", "canton": "",
        "city": "", "postal_code": postal_code, "evidence": "",
    }


def job_geography(job: dict) -> dict:
    """Décision géographique priorisant le champ lieu sur le texte de la fiche."""
    location = str(job.get("location", "") or "")
    location_geo = structured_geography(location)
    if location_geo["status"] != "unknown":
        return location_geo
    title_geo = structured_geography(job.get("title", ""))
    if title_geo["status"] != "unknown":
        return title_geo
    # Sans champ lieu exploitable, ne lire que le lieu explicitement étiqueté
    # dans la description. Les simples mentions de pays (clients, missions,
    # voyages) ne doivent pas transformer une offre locale en offre étrangère.
    hint = extract_location_hint(job.get("description", "")[:2500])
    return structured_geography(hint)


def in_zone(location: str, description: str = "") -> bool:
    """Vrai si l'offre est dans la zone Genève + Nyon proche.

    Politique stricte : une localisation dans la zone doit être vérifiable.
    - Un lieu connu hors-zone (Lausanne, Fribourg…) → rejet.
    - Un lieu de la zone (Genève, Nyon…) → accepté.
    - Aucun indice de lieu → rejet, car les boards nationaux peuvent ignorer
      leurs paramètres régionaux et renvoyer des offres de Zurich ou Berne.
    """
    return job_geography({
        "location": location,
        "description": description,
        "title": "",
    })["status"] == "target"


# Lieux plus larges à remonter seulement dans « À vérifier » pendant un passage
# de rappel élargi. On garde la sélection principale centrée Genève + Nyon proche.
GEO_REVIEW = [
    "lausanne", "gland", "rolle", "morges", "renens", "pully",
]
_GEO_REVIEW_RE = _compile_terms(GEO_REVIEW, inflect=False)


def broad_recall_enabled() -> bool:
    """Active un rappel élargi hebdomadaire, sans polluer la sélection principale."""
    setting = os.environ.get("BROAD_RECALL", "").strip().lower()
    if setting in ("1", "true", "yes", "oui", "on", "always"):
        return True
    if setting in ("0", "false", "no", "non", "off", "never"):
        return False
    days = os.environ.get("BROAD_RECALL_DAYS", "6")
    enabled_days = {
        int(part) for part in re.findall(r"\d+", days)
        if 0 <= int(part) <= 6
    }
    return local_now().weekday() in enabled_days


def geo_context(job: dict) -> str:
    """Texte court utilisé pour décider la zone, sans reprendre toute une fiche."""
    return " ".join((
        job.get("location", ""),
        job.get("title", ""),
        job.get("description", "")[:2500],
    ))


def job_in_zone(job: dict) -> bool:
    return job_geography(job)["status"] == "target"


def job_has_far_location(job: dict) -> bool:
    if structured_geography(job.get("title", ""))["status"] == "outside":
        return True
    return job_geography(job)["status"] == "outside"


def job_has_review_location(job: dict) -> bool:
    return term_in(normalize(geo_context(job)), _GEO_REVIEW_RE)


def filter_reason(job: dict) -> str:
    """Raison stable expliquant pourquoi une offre n'entre pas en sélection."""
    title = job.get("title", "")
    description = job.get("description", "")
    title_norm = normalize(title)
    if not is_french_text(title, description):
        return "langue_non_prise_en_charge"
    if term_in(title_norm, _TITLE_EXCLUDE_RE) or term_in(title_norm, _EXCLUDE_RE):
        return "metier_exclu_dans_titre"
    if is_fle(title, description):
        return "fle_exclu"
    if job_has_far_location(job):
        return "lieu_hors_zone_dans_titre"
    if not job_in_zone(job):
        return "lieu_hors_zone_ou_inconnu"
    if not strict_title_match(title):
        return "aucun_signal_metier_dans_titre"
    if relevance_score(title, description) < MIN_SCORE:
        return "score_inferieur_au_seuil"
    return ""


def review_candidate(job: dict) -> tuple[bool, list]:
    """Accepte en revue uniquement un candidat local, plausible et non exclu."""
    title = job.get("title", "")
    description = job.get("description", "")
    title_norm = normalize(title)
    if not is_french_text(title, description):
        return False, []
    if term_in(title_norm, _TITLE_EXCLUDE_RE) or term_in(title_norm, _EXCLUDE_RE):
        return False, []
    title_far = structured_geography(title)["status"] == "outside"
    title_review_far = term_in(title_norm, _GEO_REVIEW_RE)
    if is_fle(title, description):
        return False, []
    if title_far and not (
        ACTIVE_PROFILE == "lettres" and broad_recall_enabled() and title_review_far
    ):
        return False, []
    if not job_in_zone(job):
        if (ACTIVE_PROFILE == "lettres" and broad_recall_enabled()
                and job_has_review_location(job)):
            reasons = weak_relevance_reasons(title, description)
            if strict_title_match(title) or reasons:
                return True, ["lieu élargi à vérifier"] + reasons
        if job_has_far_location(job):
            return False, []
        reasons = weak_relevance_reasons(title, description)
        if strict_title_match(title) or reasons:
            return True, ["lieu à confirmer"] + reasons
        return False, []
    if strict_title_match(title):
        score = relevance_score(title, description)
        if score < MIN_SCORE:
            return True, [f"score {score} inférieur au seuil {MIN_SCORE}"]
        return False, []
    reasons = weak_relevance_reasons(title, description)
    return bool(reasons), list(dict.fromkeys(reasons))


def classify_job(job: Job) -> Decision:
    """Décision unique et inspectable pour la sélection, la revue ou le rejet."""
    if passes_filters(job):
        return {"destination": "main", "reason": "", "review_reasons": []}
    keep, reasons = review_candidate(job)
    if keep:
        reasons = list(dict.fromkeys(reasons))
        return {
            "destination": "review",
            "reason": "; ".join(reasons),
            "review_reasons": reasons,
        }
    return {
        "destination": "reject",
        "reason": filter_reason(job) or "pertinence_insuffisante",
        "review_reasons": [],
    }


def _detail_candidate_priority(candidate: dict) -> tuple:
    """Ordre stable : enrichir d'abord ce qui débloque une vraie décision."""
    title = candidate["title"]
    description = candidate["description"]
    needs_relevance = not strict_title_match(title)
    needs_geography = not candidate["geo_ok"]
    blocking_signals = int(needs_relevance) + int(needs_geography)
    seed_score = relevance_score(title, description)
    fields = candidate["fields"]
    return (
        -blocking_signals,
        -int(needs_relevance),
        -int(needs_geography),
        -seed_score,
        normalize(fields.get("source", "")),
        canonical_url(candidate["url"]),
    )


def _finish_considered_candidate(candidate: dict, details: dict | None = None):
    """Fusionne l'enrichissement puis applique une seule fois le funnel final."""
    title = candidate["title"]
    description = candidate["description"]
    fields = dict(candidate["fields"])
    details = details or {}
    if details:
        description = description or details.get("description", "")
        if details.get("location") and not candidate["geo_ok"]:
            fields["location"] = details["location"]
        if (details.get("company")
                and not is_meaningful_company(fields.get("company", ""))):
            fields["company"] = details["company"]
        for name in (
            "date_posted", "valid_through", "employment_type",
            "job_location_type", "external_id", "salary",
        ):
            if details.get(name) and not fields.get(name):
                fields[name] = details[name]
    job = {
        "title": title,
        "url": candidate["url"],
        "description": description,
        "found_at": candidate["found_at"],
        **fields,
    }
    if candidate["trusted_geo"] and not job.get("location"):
        job["location"] = "Genève"
    finalize(job)
    if fle_risk(title) and is_fle(title, description):
        record_rejection("fle_exclu", job)
        return
    decision = classify_job(job)
    if decision["destination"] == "main":
        candidate["jobs"].append(job)
        return
    if decision["destination"] == "review":
        job["_review"] = True
        job["review_reason"] = decision["reason"]
        candidate["jobs"].append(job)
        return
    record_rejection(decision["reason"], job)


def _pending_detail_count(source: str = "") -> int:
    with _COUNTERS_LOCK:
        if not source:
            return len(_pending_detail_candidates)
        return sum(
            1 for item in _pending_detail_candidates
            if item["fields"].get("source") == source
        )


def _fair_detail_order(pending: list) -> list:
    """Évite qu'une grosse source monopolise le quota à priorité égale."""
    buckets = defaultdict(lambda: defaultdict(deque))
    for candidate in sorted(pending, key=_detail_candidate_priority):
        priority = _detail_candidate_priority(candidate)
        bucket_key = priority[:4]
        source = normalize(candidate["fields"].get("source", "")) or "?"
        buckets[bucket_key][source].append(candidate)
    ordered = []
    for bucket_key in sorted(buckets):
        queues = buckets[bucket_key]
        while any(queues.values()):
            for source in sorted(
                queues, key=lambda value: (-_detail_source_yield.get(value, 0), value)
            ):
                if queues[source]:
                    ordered.append(queues[source].popleft())
    return ordered


def _process_pending_detail_candidates():
    """Enrichit les candidats après la collecte, dans un ordre reproductible."""
    with _COUNTERS_LOCK:
        pending = list(_pending_detail_candidates)
        _pending_detail_candidates.clear()
    before = _detail_fetch_count
    for candidate in _fair_detail_order(pending):
        try:
            details = fetch_detail_fields(candidate["url"], candidate["title"])
            _finish_considered_candidate(candidate, details)
        except Exception as exc:
            log(
                f"⚠️  Enrichissement impossible "
                f"({candidate['fields'].get('source', '?')}): {exc}"
            )
            _finish_considered_candidate(candidate, {})
    if pending:
        log(
            f"Enrichissement différé : {len(pending)} candidat(s) classé(s), "
            f"{_detail_fetch_count - before} fiche(s) téléchargée(s), "
            f"quota {MAX_DETAIL_FETCHES}."
        )


def consider(title: str, url: str, base_fields: dict, jobs: list, seen_urls: set):
    """Logique commune : pertinence (titre puis description si ambigu),
    filtre géographique, enrichissement et ajout.

    base_fields doit contenir au moins company, source, location.
    """
    if not title or not url:
        return
    fields = dict(base_fields)
    query = fields.pop("_query", "")
    no_fetch = fields.pop("_no_fetch", False)
    trusted_geo = fields.pop("_trusted_geo", False)
    raw_recorded = fields.pop("_raw_recorded", False)
    health_source = fields.pop("_health_source", "")
    src = fields.get("source", "?")
    # Candidat brut avant filtres. Verrouillé car les scrapers HTTP tournent en parallèle.
    if not raw_recorded:
        record_raw_candidate(src)
        if health_source and health_source != src:
            record_raw_candidate(health_source)
    record_query_candidate(src, query)
    if url in seen_urls:
        return
    seed_job = {
        "title": title,
        "url": url,
        "description": fields.pop("description", ""),
        **fields,
    }
    if not is_french_text(title, seed_job.get("description", "")):
        record_rejection("langue_non_prise_en_charge", seed_job)
        return
    title_norm = normalize(title)
    if term_in(title_norm, _TITLE_EXCLUDE_RE) or term_in(title_norm, _EXCLUDE_RE):
        record_rejection("metier_exclu_dans_titre", seed_job)
        return
    title_far = structured_geography(title)["status"] == "outside"
    title_review_far = term_in(title_norm, _GEO_REVIEW_RE)
    if title_far and not (
        ACTIVE_PROFILE == "lettres" and broad_recall_enabled() and title_review_far
    ):
        record_rejection("lieu_hors_zone_dans_titre", seed_job)
        return
    seen_urls.add(url)
    description = seed_job.get("description", "")
    plausible = (
        strict_title_match(title)
        or title_is_ambiguous(title)
        or bool(weak_relevance_reasons(title, description))
    )
    if not plausible:
        record_rejection("pertinence_insuffisante", seed_job)
        return
    geo_ok = trusted_geo or in_zone(fields.get("location", ""), title + " " + description)
    needs_details = FETCH_LOCAL_DETAILS and FETCH_DESCRIPTIONS and not no_fetch and (
        not description or not geo_ok or not is_meaningful_company(fields.get("company", ""))
    )
    candidate = {
        "title": title,
        "url": url,
        "description": description,
        "fields": fields,
        "trusted_geo": trusted_geo,
        "geo_ok": geo_ok,
        "found_at": local_now().isoformat(),
        "jobs": jobs,
    }
    if needs_details and _DEFER_DETAIL_FETCHES:
        with _COUNTERS_LOCK:
            _pending_detail_candidates.append(candidate)
        return
    details = fetch_detail_fields(url, title) if needs_details else {}
    _finish_considered_candidate(candidate, details)


# ---------------------------------------------------------------------------
# Scrapers existants (inchangés sauf intégration de consider/finalize)
# ---------------------------------------------------------------------------

def scrape_ville_geneve() -> list:
    """Offres de la Ville de Genève (administration municipale).

    NB : ville-geneve.ch redirige désormais vers geneve.ch. On construit donc
    les liens de détail sur geneve.ch directement, pour éviter les 404.
    """
    jobs, seen_urls = [], set()
    url = (
        "https://www.geneve.ch/autorites-administration/"
        "administration-municipale/travailler-ville-geneve/offres-emploi/"
    )
    soup = fetch(url)
    if not soup:
        return jobs
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/administration-municipale/offres-emploi/" not in href and "/offres-emploi/" not in href:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        if not href.startswith("http"):
            href = "https://www.geneve.ch" + href
        # On force le domaine geneve.ch (ville-geneve.ch renvoie des 404 en détail)
        href = href.replace("https://www.ville-geneve.ch", "https://www.geneve.ch")
        consider(title, href,
                 {"company": "Ville de Genève", "source": "geneve.ch",
                  "location": "Genève"}, jobs, seen_urls)
    log(f"geneve.ch (Ville): {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _parse_letemps_listing(html: str, base_url: str) -> list:
    """Parse le listing actuel, dont les fiches utilisent `/emploi/<uuid>`."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    detail_re = re.compile(r"^/emploi/[0-9a-f-]{30,}/?$", re.I)
    for anchor in soup.select("a[href]"):
        full_url = urljoin(base_url, anchor.get("href", ""))
        if not detail_re.match(urlparse(full_url).path):
            continue
        key = canonical_url(full_url)
        title = _job_anchor_title(anchor)
        if not title or key in seen:
            continue
        node, card_text = anchor, title
        for _ in range(6):
            parent = getattr(node, "parent", None)
            if parent is None:
                break
            text_value = parent.get_text(" ", strip=True)
            if len(text_value) > 1200:
                break
            node, card_text = parent, text_value
            if getattr(parent, "name", "") in ("article", "li"):
                break
        geo = structured_geography(card_text)
        location = display_location(geo["evidence"].title()) if geo["status"] != "unknown" else ""
        company = "—"
        for image in node.select("img[alt]") if hasattr(node, "select") else ():
            match = re.search(r"offre proposée par\s+(.+)", image.get("alt", ""), re.I)
            if match:
                company = match.group(1).strip()[:120]
                break
        seen.add(key)
        offers.append({
            "title": title, "url": full_url, "company": company,
            "location": location, "description": card_text[:1000],
        })
    return offers


def scrape_letemps() -> list:
    """Le Temps Emploi — page de listing."""
    jobs, seen_urls = [], set()
    url = "https://www.letemps.ch/emploi"
    soup = fetch(url)
    if not soup:
        return jobs
    for offer in _parse_letemps_listing(str(soup), url):
        consider(
            offer["title"], offer["url"],
            {"company": offer["company"], "source": "Le Temps Emploi",
             "location": offer["location"], "description": offer["description"]},
            jobs, seen_urls,
        )
    log(f"Le Temps Emploi: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_vaud() -> list:
    """Offres de l'État de Vaud via Oracle HCM REST API."""
    SOURCE = "offres-emploi.vd.ch"
    jobs = []
    mark_raw_source(SOURCE)
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
        r = session().get(api_url, headers=headers_oracle, timeout=20)
        r.raise_for_status()
        data = r.json()
        reqs = data.get("items", [{}])[0].get("requisitionList", [])
        found = 0
        for job in reqs:
            title = job.get("Title", "").strip()
            jid = job.get("Id")
            if not title or not jid:
                continue
            record_raw_candidate(SOURCE)
            short_desc = job.get("ShortDescriptionStr") or ""
            loc = job.get("PrimaryLocation", "")
            if not any(z in loc.lower() for z in VAUD_ZONE):
                continue
            if not is_french_text(title):
                log(f"Rejeté (langue non-FR) : {title[:70]}")
                continue
            if is_relevant(title, short_desc):
                j = {
                    "title": title, "company": "État de Vaud",
                    "url": f"https://offres-emploi.vd.ch/#fr/job/{jid}",
                    "source": SOURCE, "location": loc,
                    "external_id": str(jid),
                    "description": short_desc,
                    "found_at": local_now().isoformat(),
                }
                jobs.append(finalize(j))
                found += 1
        log(f"offres-emploi.vd.ch: {found} offre(s) trouvée(s) sur {len(reqs)} total")
    except Exception as e:
        log(f"Erreur scrape_vaud: {e}")
    return jobs


def _parse_jobscout24_page(html: str, base: str, fallback_location: str = "",
                           zone_filter: set | None = None) -> list:
    """Parse une page JobScout24 sans réseau, pour tests et scraper réel."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    links = soup.select("a.job-link-detail, a.job-title, a[href*='/fr/job/']")
    for link in links:
        href = link.get("href", "")
        if "/fr/job/" not in href:
            continue
        title = (link.get("title", "") or link.get_text(strip=True)).strip()
        if not title:
            continue
        full_url = urljoin(base + "/", href)
        url_key = canonical_url(full_url)
        if url_key in seen:
            continue
        container = link.find_parent(["li", "article"]) or _job_card(link)
        location = container.get_text(" ", strip=True)[:300] if container else ""
        if container:
            spans = container.select("p.job-attributes span, .job-location, .location")
            if spans:
                texts = [span.get_text(strip=True) for span in spans]
                location = texts[1] if len(texts) > 1 else texts[0]
        # Le filtre de région du portail n'est pas une preuve suffisante : il a
        # déjà renvoyé des cartes de toute la Suisse. On conserve donc le lieu
        # extrait tel quel au lieu de transformer un lieu inconnu en Genève.
        if zone_filter is not None and not any(
            normalize(place) in normalize(location) for place in zone_filter
        ):
            continue
        seen.add(url_key)
        offers.append({"title": title, "url": full_url, "location": location})
    return offers


def scrape_jobscout24() -> list:
    """Offres privées via JobScout24.ch.

    Sélecteur robuste : on cible directement les liens d'offres par leur motif
    d'URL (/fr/job/) plutôt que de dépendre du conteneur <li> parent, qui peut
    changer. Le lieu/entreprise est lu dans le conteneur le plus proche.
    """
    KEYWORDS_JS24 = [
        "redacteur", "editeur", "bibliothecaire", "libraire", "correcteur",
        "traducteur", "documentaliste", "journaliste", "communication",
        "edition", "professeur-francais", "enseignant", "mediateur-culturel",
        "charge-de-communication", "archiviste", "musee", "patrimoine",
        "mediation", "charge-de-projet-culturel",
        # Ajouts recall (profil Lettres)
        "professeur-de-lettres", "enseignant-de-francais", "relecteur",
        "assistant-editorial", "charge-edition", "mediathecaire",
        "guide-conferencier", "redacteur-technique", "concepteur-redacteur",
    ]
    BASE = "https://www.jobscout24.ch"
    jobs, seen_urls = [], set()
    # Le passage GE laisse le funnel lire la fiche si le lieu de la carte est
    # ambigu ; le passage VD reste limité aux communes nyonnaises admises.
    search_configs = [("GE", None, ""), ("VD", VAUD_ZONE, "")]

    for kw in source_terms("jobscout24", KEYWORDS_JS24):
        mark_query("jobscout24.ch", kw)
        for region_code, zone_filter, fallback_location in search_configs:
            url = f"{BASE}/fr/jobs/{kw}/?region={region_code}"
            if not robots_allows(url):
                continue
            try:
                _polite_wait(url)
                r = session().get(url, timeout=15)
                if r.status_code != 200:
                    log(
                        f"Erreur jobscout24 [{kw}/{region_code}] : "
                        f"HTTP {r.status_code}"
                    )
                    continue
                for offer in _parse_jobscout24_page(
                    r.text, BASE, fallback_location, zone_filter
                ):
                    # Le filtre géographique fin est de toute façon dans consider()
                    consider(offer["title"], offer["url"],
                             {"company": "—", "source": "jobscout24.ch",
                              "location": offer["location"], "_query": kw,
                              "_trusted_geo": False}, jobs, seen_urls)
            except Exception as e:
                log(f"Erreur jobscout24 [{kw}/{region_code}]: {e}")

    _warn_if_empty("jobscout24.ch", jobs)
    log(f"jobscout24.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _parse_jobup_page(html: str, base: str, fallback_location: str = "",
                      zone_filter: set | None = None) -> list:
    """Parse les cartes Jobup à partir d'un instantané HTML."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for card in soup.select("[data-cy='serp-item']"):
        link = card.select_one("[data-cy='job-link']")
        if not link:
            continue
        title = link.get("title", "").strip()
        href = link.get("href", "")
        if not title or not href:
            continue
        full_url = urljoin(base + "/", href)
        url_key = canonical_url(full_url)
        if url_key in seen:
            continue
        card_text = card.get_text("\n")
        location = ""
        if "Lieu de travail" in card_text:
            after = card_text.split("Lieu de travail", 1)[1]
            location = after.strip().lstrip(":").split("\n")[0].strip()[:60]
        location = location or fallback_location
        if zone_filter is not None and not any(
            normalize(place) in normalize(location) for place in zone_filter
        ):
            continue
        seen.add(url_key)
        offers.append({"title": title, "url": full_url, "location": location})
    return offers


def scrape_jobup() -> list:
    """Offres via jobup.ch (HTML server-side).

    Si jobup bascule en rendu JS/API (interdit par robots.txt), ce scraper
    retournera 0 et l'auto-diagnostic le signalera. Repli possible : sitemap
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
    SEARCH_CONFIGS = [("region=34", None, "Genève"), ("location=nyon", VAUD_ZONE, "Nyon")]
    jobs, seen_urls = [], set()

    for kw in source_terms("jobup", KEYWORDS_JU):
        mark_query("jobup.ch", kw)
        for geo_param, zone_filter, fallback_location in SEARCH_CONFIGS:
            url = f"{BASE}/fr/emplois/?{geo_param}&term={quote(kw)}"
            if not robots_allows(url):
                continue
            try:
                _polite_wait(url)
                r = session().get(url, timeout=15)
                if r.status_code != 200:
                    log(
                        f"Erreur jobup [{kw}/{geo_param}] : HTTP {r.status_code}"
                    )
                    continue
                for offer in _parse_jobup_page(
                    r.text, BASE, fallback_location, zone_filter
                ):
                    consider(offer["title"], offer["url"],
                             {"company": "—", "source": "jobup.ch",
                              "location": offer["location"], "_query": kw,
                              "_trusted_geo": True}, jobs, seen_urls)
            except Exception as e:
                log(f"Erreur jobup [{kw}/{geo_param}]: {e}")

    _warn_if_empty("jobup.ch", jobs)
    log(f"jobup.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _transient_retry_delay(response, attempt: int) -> float:
    """Délai borné, en respectant Retry-After lorsqu'il est fourni."""
    value = str(response.headers.get("Retry-After", "")).strip()
    try:
        return min(30.0, max(0.0, float(value)))
    except ValueError:
        return float(2 ** attempt)


def _adzuna_get(url: str, retries: int = 2):
    """GET Adzuna avec reprise courte sur surcharge, sans journaliser les clés."""
    last_response = None
    for attempt in range(max(1, retries)):
        _polite_wait(url)
        try:
            response = session().get(url, timeout=15)
        except requests.RequestException:
            if attempt + 1 >= retries:
                raise
            time.sleep(float(2 ** attempt))
            continue
        last_response = response
        if response.status_code not in _TRANSIENT_HTTP_STATUSES:
            return response
        if attempt + 1 < retries:
            time.sleep(_transient_retry_delay(response, attempt))
    return last_response


def scrape_adzuna() -> list:
    """Offres via l'API Adzuna — agrège Indeed, LinkedIn, etc."""
    if not ADZUNA_ID or not ADZUNA_KEY:
        log("Adzuna : identifiants absents (ADZUNA_ID/ADZUNA_KEY) — source ignorée")
        return []
    KEYWORDS_AZ = [
        "rédacteur", "éditeur", "bibliothécaire", "libraire", "correcteur",
        "traducteur", "journaliste", "documentaliste", "professeur français",
        "communication culturelle", "archiviste", "médiateur culturel",
        "chargé de projet culturel",
        # Ajouts recall (profil Lettres)
        "professeur de lettres", "assistant éditorial", "relecteur",
        "médiathécaire", "chargé de médiation", "rédacteur technique",
    ]
    jobs, seen_urls = [], set()
    consecutive_unavailable = 0
    for kw in source_terms("adzuna", KEYWORDS_AZ):
        mark_query("Adzuna (Indeed+)", kw)
        url = (
            "https://api.adzuna.com/v1/api/jobs/ch/search/1"
            f"?app_id={ADZUNA_ID}&app_key={ADZUNA_KEY}"
            f"&results_per_page=50&what={quote(kw)}"
            "&where=Geneva&distance=30&max_days_old=30"
            "&content-type=application/json"
        )
        try:
            r = _adzuna_get(url)
            if r.status_code != 200:
                log(f"Erreur Adzuna [{kw}] : HTTP {r.status_code}")
                if r.status_code in _TRANSIENT_HTTP_STATUSES:
                    consecutive_unavailable += 1
                    if consecutive_unavailable >= 2:
                        log(
                            "Adzuna temporairement indisponible : arrêt des "
                            "requêtes restantes pour ce passage"
                        )
                        break
                continue
            consecutive_unavailable = 0
            for item in r.json().get("results", []):
                title = item.get("title", "").strip()
                link = item.get("redirect_url", "")
                desc = item.get("description", "")[:600]
                company = item.get("company", {}).get("display_name", "—")
                location = item.get("location", {}).get("display_name", "—")
                dedup_key = urlparse(link).path
                if not title or not link or dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)
                consider(
                    title, link,
                    {"company": company, "source": "Adzuna (Indeed+)",
                     "location": location, "description": desc, "_query": kw,
                     # L'API fournit déjà les champs utiles et ses redirections
                     # publicitaires sont interdites par robots.txt.
                     "_no_fetch": True},
                    jobs, set(),
                )
        except requests.RequestException as exc:
            consecutive_unavailable += 1
            log(f"Adzuna [{kw}] : erreur réseau ({type(exc).__name__})")
            if consecutive_unavailable >= 2:
                log(
                    "Adzuna temporairement indisponible : arrêt des "
                    "requêtes restantes pour ce passage"
                )
                break
        except (ValueError, TypeError) as exc:
            log(f"Adzuna [{kw}] : réponse JSON invalide ({type(exc).__name__})")
    log(f"Adzuna: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# --- Script d'init « furtif » : masque les marqueurs d'automatisation que les
# murs anti-bot (myScience /bot_score, DataDome d'Indeed…) inspectent. ---
_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => false});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-CH', 'fr']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
window.chrome = window.chrome || {runtime: {}};
"""


def _new_stealth_context(browser):
    """Crée un contexte Chromium réaliste + furtif (réutilisé par plusieurs sources)."""
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="fr-CH",
        timezone_id="Europe/Zurich",
    )
    ctx.add_init_script(_STEALTH_INIT_JS)
    return ctx


def _launch_chromium(playwright):
    """Lance le Chromium Playwright, puis un éventuel navigateur système natif."""
    explicit_path = os.environ.get("CHROMIUM_EXECUTABLE_PATH", "").strip()
    if explicit_path:
        if _is_snap_chromium(explicit_path):
            raise PlaywrightBrowserUnavailable(
                "CHROMIUM_EXECUTABLE_PATH désigne Chromium Snap, non pris en charge"
            )
        if not _is_executable_file(explicit_path):
            raise PlaywrightBrowserUnavailable(
                f"CHROMIUM_EXECUTABLE_PATH invalide : {explicit_path}"
            )
        return playwright.chromium.launch(
            headless=True, executable_path=explicit_path
        )

    # Le navigateur géré par Playwright est prioritaire : sa version correspond
    # exactement à celle de la bibliothèque Python et fonctionne aussi en CI.
    managed_path = str(playwright.chromium.executable_path or "")
    if _is_executable_file(managed_path):
        return playwright.chromium.launch(
            headless=True, executable_path=managed_path
        )

    system_path = _CHROMIUM_PATH or _find_system_chromium()
    if system_path:
        return playwright.chromium.launch(
            headless=True, executable_path=system_path
        )

    raise PlaywrightBrowserUnavailable(
        "Chromium Playwright absent. Mise à niveau/installation : "
        "./venv/bin/python3 -m pip install -U 'playwright>=1.61,<2', puis "
        "./venv/bin/python3 -m playwright install --with-deps chromium"
    )


_PLAYWRIGHT_FAILURE_LOG_LOCK = threading.Lock()
_PLAYWRIGHT_FAILURES_REPORTED = set()


def _playwright_error_summary(exc: Exception) -> str:
    """Conserve la cause utile sans recopier les centaines de lignes du navigateur."""
    first_line = next(
        (line.strip() for line in str(exc).splitlines() if line.strip()),
        type(exc).__name__,
    )
    return first_line[:500]


def _log_playwright_failure(url: str, exc: Exception):
    """Journalise une seule fois la même panne Playwright par site et profil."""
    summary = _playwright_error_summary(exc)
    host = urlparse(url).netloc or url
    key = (ACTIVE_PROFILE, host, type(exc).__name__, summary)
    with _PLAYWRIGHT_FAILURE_LOG_LOCK:
        if key in _PLAYWRIGHT_FAILURES_REPORTED:
            return
        _PLAYWRIGHT_FAILURES_REPORTED.add(key)
    suffix = (
        " — source ignorée"
        if isinstance(exc, PlaywrightBrowserUnavailable)
        else ""
    )
    log(f"Erreur Playwright {host} : {summary}{suffix}")


def fetch_via_playwright(url: str, wait_selector: str = None,
                         wait_until: str = "domcontentloaded"):
    """Charge une page derrière un mur JS via un Chromium furtif.

    Laisse le vrai navigateur exécuter le défi anti-bot (ex. POST /bot_score de
    myScience) puis renvoie le HTML rendu sous forme de BeautifulSoup.
    Retourne None si Chromium est indisponible ou en cas d'échec.
    """
    cached_html = _run_cached_html(url)
    if cached_html is not None:
        return BeautifulSoup(cached_html, "lxml")
    if not PLAYWRIGHT_AVAILABLE:
        _log_playwright_failure(
            url,
            PlaywrightBrowserUnavailable("bibliothèque Playwright non installée"),
        )
        return None
    try:
        with _sync_playwright() as pw:
            browser = _launch_chromium(pw)
            ctx = _new_stealth_context(browser)
            page = ctx.new_page()
            try:
                # `networkidle` ne se produit jamais sur certains sites qui
                # gardent analytics/WebSocket ouverts (notamment CAGI).
                page.goto(url, wait_until=wait_until, timeout=25000)
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=8000)
                    except Exception:
                        pass            # le défi a pu rediriger sans ce sélecteur
                else:
                    page.wait_for_timeout(4000)
                html = page.content()
            finally:
                browser.close()
        _remember_run_html(url, html)
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        _log_playwright_failure(url, e)
        return None


INDEED_QUERIES = [
    ("rédacteur", "Genève"), ("éditeur", "Genève"), ("correcteur", "Genève"),
    ("bibliothécaire", "Genève"), ("traducteur", "Genève"),
    ("médiateur culturel", "Genève"), ("archiviste", "Genève"),
    ("journaliste", "Genève"), ("chargé de projet culturel", "Genève"),
]


def scrape_indeed_pw() -> list:
    """Offres Indeed CH via Playwright. Nécessite Chromium Playwright."""
    if not ENABLE_INDEED:
        return []                # désactivé par défaut (anti-bot) — cf. ENABLE_INDEED
    if not PLAYWRIGHT_AVAILABLE:
        log("Indeed : Playwright non installé — source ignorée")
        return []
    jobs, seen_urls = [], set()
    with _sync_playwright() as pw:
        browser = _launch_chromium(pw)
        ctx = _new_stealth_context(browser)      # contexte furtif partagé
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
                    location = loc_el.get_text(strip=True) if loc_el else ""
                    if not is_french_text(title):
                        log(f"Rejeté (langue non-FR) : {title[:70]}")
                        continue
                    if not is_relevant(title):
                        continue
                    seen_urls.add(full_url)
                    j = {
                        "title": title, "company": "—", "url": full_url,
                        "source": "Indeed CH", "location": location,
                        "description": "",
                        "found_at": local_now().isoformat(),
                    }
                    jobs.append(finalize(j))
            except Exception as e:
                _log_playwright_failure(url, e)
            time.sleep(2)
        browser.close()
    log(f"Indeed CH (Playwright): {len(jobs)} offre(s) trouvée(s)")
    return jobs


# Mots-clés ciblés profil Lettres pour jobs.ch, + noms d'écoles privées genevoises
# (elles publient sur jobs.ch — cf. découverte ; une requête par école capte leurs
# postes de français/lettres). Bornés pour limiter le nombre de requêtes.
JOBS_CH_QUERIES = [
    "enseignant français", "professeur français", "professeur de lettres",
    "bibliothécaire", "archiviste", "documentaliste", "médiathécaire",
    "rédacteur", "correcteur", "assistant éditorial",
    "médiateur culturel", "chargé de projet culturel",
    # Écoles privées genevoises
    "Florimont", "Collège du Léman", "École Moser",
    "Institut International de Lancy",
]
# Préfixe de date relative collé au titre dans la liste jobs.ch.
_JOBSCH_DATE_RE = re.compile(
    r"^(Aujourd'hui|Avant-hier|Hier|La semaine dernière|"
    r"Il y a \d+\s+(?:minute|heure|jour|semaine|mois|an)s?)\s+", re.I)


def _parse_jobs_ch_anchor(raw: str):
    """Extrait (titre, lieu) du libellé d'un lien jobs.ch (date + titre + lieu collés)."""
    title = _JOBSCH_DATE_RE.sub("", raw)
    title = re.split(r"Lieu de travail|Taux d'|Salaire", title)[0].strip()
    m = re.search(r"Lieu de travail\s*:?\s*(.+?)(?:\s+Taux|\s+Salaire|$)", raw)
    location = m.group(1).strip() if m else ""
    return title, location


def _parse_jobs_ch_page(html: str, base: str = "https://www.jobs.ch") -> list:
    """Parse les cartes jobs.ch rendues par Playwright, sans navigateur."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select('[data-cy="serp-item"]')
    if not cards:
        cards = soup.select('a[href*="/offres-emplois/detail/"]')
    offers, seen = [], set()
    for card in cards:
        anchor = (
            card.select_one('a[data-cy="job-link"]')
            or (card if card.name == "a"
                else card.select_one('a[href*="/offres-emplois/detail/"]'))
        )
        if not anchor or not anchor.get("href"):
            continue
        href = anchor["href"]
        if "/offres-emplois/detail/" not in href:
            continue
        full_url = urljoin(base + "/", href)
        url_key = canonical_url(full_url)
        if url_key in seen:
            continue
        title, location = _parse_jobs_ch_anchor(
            anchor.get_text(" ", strip=True)
        )
        if not title or len(title) < 4:
            continue
        seen.add(url_key)
        offers.append({
            "title": title,
            "url": full_url,
            "location": location or "Genève",
        })
    return offers


def scrape_jobs_ch_pw() -> list:
    """Offres jobs.ch via Playwright (rendu JS). Best-effort.

    Plus gros board suisse, mais protégé (Cloudflare) et recouvrant largement
    jobscout24 (même groupe JobCloud — la fusion des doublons par titre+employeur
    regroupe les annonces communes). Nécessite Chromium Playwright.

    NB : jobs.ch fait de la recherche *sémantique* — une requête niche (ex.
    « bibliothécaire ») ramène aussi des offres voisines hors-sujet, écartées
    par is_relevant(). Un total de 0 sur un run = aucune offre Lettres en zone
    ce jour-là, PAS un sélecteur cassé (vérifié : les cartes se parsent bien).
    """
    if not PLAYWRIGHT_AVAILABLE:
        log("jobs.ch : Playwright non installé — source ignorée")
        return []
    jobs, seen_urls = [], set()
    with _sync_playwright() as pw:
        browser = _launch_chromium(pw)
        ctx = _new_stealth_context(browser)
        page = ctx.new_page()
        for term in source_terms("jobs_ch", JOBS_CH_QUERIES):
            mark_query("jobs.ch", term)
            url = (f"https://www.jobs.ch/fr/offres-emplois/"
                   f"?term={quote(term)}&location={quote('Genève')}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.wait_for_selector('a[href*="/offres-emplois/detail/"]',
                                           timeout=8000)
                except Exception:
                    pass
                for offer in _parse_jobs_ch_page(page.content()):
                    consider(
                        offer["title"], offer["url"],
                        {"company": "", "source": "jobs.ch",
                         "location": offer["location"],
                         "_query": term, "_trusted_geo": True},
                        jobs, seen_urls,
                    )
            except Exception as e:
                _log_playwright_failure(url, e)
            time.sleep(2)
        browser.close()
    log(f"jobs.ch (Playwright): {len(jobs)} offre(s) trouvée(s)")
    return jobs


# Établissement employeur sur la page de détail ge.ch : « Lieu de travail
# <établissement> Postuler ». SÉLECTEUR À AJUSTER SI BESOIN.
_GE_LIEU_RE = re.compile(
    r"Lieu de travail\s+(.+?)\s+(?:Postuler|Type de publication)\b", re.S)


def _ge_ch_etablissement(href: str) -> str:
    """Établissement (« École de commerce Nicolas-Bouvier ») depuis la page de détail.

    Sert de SIGNAL DE DÉDUP : deux postes au même intitulé mais dans deux écoles
    différentes de l'État de Genève doivent rester distincts. Retourne "" si rien.
    """
    soup = fetch(href, retries=2)
    if not soup:
        return ""
    m = _GE_LIEU_RE.search(soup.get_text(" ", strip=True))
    if not m:
        return ""
    name = re.sub(r"[\s​]+", " ", m.group(1)).strip(" ,;:.​")
    return name if 1 < len(name) <= 80 else ""


def scrape_ge_ch() -> list:
    """Offres de l'État de Genève.

    Chaque offre RETENUE est ensuite rattachée à son établissement (lu sur la page
    de détail) : sans cela, deux postes distincts au même intitulé générique
    (« … / Français ») sont fusionnés à tort par la dédup, faute d'employeur
    distinctif. L'enrichissement se fait après le filtrage pour ne lire que les
    pages des offres pertinentes (la liste brute compte ~40 articles).
    """
    jobs, seen_urls = [], set()
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
        consider(title, href,
                 {"company": "État de Genève", "source": "ge.ch",
                  "location": "Genève"}, jobs, seen_urls)
    # Employeur distinctif (= établissement) sur les seules offres retenues.
    for job in jobs:
        etab = _ge_ch_etablissement(job["url"])
        if etab:
            job["company"] = f"État de Genève — {etab}"
    log(f"ge.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# NOUVEAUX SCRAPERS (point 2)
# Sélecteurs « best effort » : isolés en tête de fonction, faciles à ajuster.
# Si une source renvoie 0, l'auto-diagnostic le signale dans les logs.
# ---------------------------------------------------------------------------

def scrape_unige() -> list:
    """Postes vacants de l'Université de Genève (jobs.unige.ch).

    Le portail « wd_portal » liste les postes par date. On essaie plusieurs
    URLs car le paramétrage exact varie. Les liens de détail pointent vers
    wd_portal.show_job (ou variantes).
    """
    jobs, seen_urls = [], set()
    # Plusieurs variantes d'URL de résultats (le portail accepte différents jeux
    # de paramètres ; on tente la plus simple puis une plus complète).
    urls = [
        "https://jobs.unige.ch/www/wd_portal.list?p_web_site_id=1",
        ("https://jobs.unige.ch/www/wd_portal.search_results"
         "?p_web_site_id=1&p_category_id=1&p_show_results=Y"
         "&p_form_type=CHECKBOX&p1=51&p1_val=Any&p2=46&p2_val=Any"
         "&p_text=&p_save_search=N"),
    ]
    for url in urls:
        soup = fetch(url)
        if not soup:
            continue
        # --- SÉLECTEUR À AJUSTER SI BESOIN ---
        candidates = soup.select(
            "a[href*='show_job'], a[href*='wd_portal'], "
            "table a[href], li a[href]"
        )
        for a in candidates:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            # On écarte les liens de navigation du portail
            if not title or len(title) < 8 or not href:
                continue
            low = title.lower()
            if any(skip in low for skip in ("recherche", "connexion", "retour",
                                            "accueil", "english", "imprimer")):
                continue
            full_url = urljoin("https://jobs.unige.ch/www/", href)
            consider(title, full_url,
                     {"company": "Université de Genève", "source": "jobs.unige.ch",
                      "location": "Genève"}, jobs, seen_urls)
        if jobs:
            break          # une URL a fonctionné, inutile de tenter la suivante
    _warn_if_empty("jobs.unige.ch", jobs)
    log(f"jobs.unige.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_myscience() -> list:
    """Postes académiques de TOUTES les universités suisses via myscience.ch.

    Couvre UNIGE, UNIL, EPFL, etc. d'un coup. On filtre sur la zone lémanique
    et la pertinence Lettres. Source précieuse car centralisée.
    """
    jobs, seen_urls = [], set()
    # Le portail liste les annonces par CATÉGORIE (et non par ?search=, qui
    # renvoie 404). On parcourt les catégories proches des Lettres Modernes ;
    # la pertinence fine reste filtrée par consider().
    categories = [
        "Linguistics-Literature", "Education", "Pedagogy", "Art-Design",
        "History-Archeology", "Media", "Philosophy", "Social+Sciences",
    ]
    for cat in categories:
        url = f"https://www.myscience.ch/fr/jobs/{cat}"
        # myScience sert un mur anti-bot JS (« Security check » → /bot_score) :
        # un simple requests ne voit jamais la liste. On passe par un Chromium
        # furtif qui exécute le défi puis rend le HTML réel.
        soup = fetch_via_playwright(url, wait_selector="a[href*='/jobs/id']")
        if not soup:
            continue
        # Les vraies annonces ont le motif /jobs/id<NNNNN>-… et sont balisées en
        # microdata schema.org : titre = span[itemprop=name], lieu = span.location.
        for a in soup.select("a[href*='/jobs/id']"):
            href = a.get("href", "")
            title_el = a.select_one("span[itemprop='name'], .results_title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or len(title) < 6 or not href:
                continue
            loc_el = a.select_one("span.location, .results_location")
            location = loc_el.get_text(strip=True) if loc_el else "Suisse"
            org_el = a.select_one(".results_organization")
            company = org_el.get_text(strip=True) if org_el else "Université (myScience)"
            full_url = urljoin("https://www.myscience.ch", href)
            consider(title, full_url,
                     {"company": company, "source": "myscience.ch",
                      "location": location}, jobs, seen_urls)
    _warn_if_empty("myscience.ch", jobs)
    log(f"myscience.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_museums() -> list:
    """Offres d'emploi dans les musées suisses (museums.ch / ICOM Suisse).

    Publication gratuite, accès propre. Cœur de cible pour médiation/édition/
    conservation. Les offres sont sous /portail-de-lemploi/<titre>-<id>.html
    """
    jobs, seen_urls = [], set()
    # Vraie URL du portail de l'emploi (et non l'URL inventée précédente)
    url = "https://www.museums.ch/fr/espace-professionnel/offres/portail-de-lemploi-3036.html"
    soup = fetch(url)
    if not soup:
        _warn_if_empty("museums.ch", jobs)
        return jobs
    # --- SÉLECTEUR À AJUSTER SI BESOIN ---
    # Les annonces sont des liens vers .../portail-de-lemploi/...-NNNN.html
    candidates = soup.select("a[href*='portail-de-lemploi/']")
    if not candidates:
        candidates = soup.select("article a[href], h2 a[href], h3 a[href], li a[href]")
    for a in candidates:
        href = a.get("href", "")
        if not href:
            continue
        # Chercher d'abord un titre court dans les enfants de l'ancre
        heading = a.select_one("h2, h3, h4, strong, span.title")
        raw = heading.get_text(strip=True) if heading else a.get_text(strip=True)
        # Supprimer le préfixe "Publié le: JJ.MM.AAAA" injecté par le CMS
        title = re.sub(r'^Publié\s+le\s*:\s*\d{2}\.\d{2}\.\d{4}', '', raw).strip()
        title = title.split("\n")[0].strip()  # première ligne seulement
        if not title or len(title) < 6 or len(title) > 150:
            continue
        full_url = urljoin("https://www.museums.ch/", href)
        consider(title, full_url,
                 {"company": "Musée (museums.ch)", "source": "museums.ch",
                  "location": "Suisse"}, jobs, seen_urls)
    _warn_if_empty("museums.ch", jobs)
    log(f"museums.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# Successeur du portail enseignant educa.Job (job.educa.ch), HORS LIGNE depuis 2025
# (NXDOMAIN). On repointe « educa » vers le portail de recrutement de la HES-SO
# Genève : vivant, rendu côté serveur (Next.js SSR → simple fetch, pas de
# Playwright), déjà ciblé Genève. Couvre le supérieur genevois (HEAD art/design,
# HEM musique, HETS travail social, HEG, HEPIA, HEdS) : chargé·e de cours,
# assistant·e HES, adjoint·e scientifique/artistique… — complète unige
# (université) sans la recouper.
HESGE_OFFERS_URL = "https://recrutement.hesge.ch/fr/offres"


def scrape_educa() -> list:
    """Offres HES-SO Genève (recrutement.hesge.ch) — supérieur / académique / artistique.

    Repointage de l'ancien educa.Job (job.educa.ch hors ligne depuis 2025). Chaque
    offre est un lien « /nos-offres/<slug>-<id> » dont le texte EST déjà le titre
    propre. consider() applique langue + pertinence + zone ; le canari de santé
    (compte brut par source) gère la détection de panne, donc pas de _warn_if_empty
    ici (un 0 PERTINENT est légitime : la plupart des postes HES sont hors profil).
    SÉLECTEUR À AJUSTER SI BESOIN : a[href*="/nos-offres/"].
    """
    jobs, seen_urls = [], set()
    mark_raw_source("recrutement.hesge.ch")
    soup = fetch(HESGE_OFFERS_URL)
    if not soup:
        return jobs
    seen_hrefs = set()
    for a in soup.select('a[href*="/nos-offres/"]'):
        href = a.get("href", "")
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 5 or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        full_url = urljoin(HESGE_OFFERS_URL, href)
        consider(title, full_url,
                 {"company": "HES-SO Genève", "source": "recrutement.hesge.ch",
                  "location": "Genève"}, jobs, seen_urls)
    log(f"recrutement.hesge.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# Educh a supprimé l'extension « .html » de ses nouvelles fiches en 2026, tout
# en la conservant sur les anciennes. Le chemin doit donc accepter les 2 formes.
_EDUCH_OFFER_RE = re.compile(
    r"/emploi/[^/?#]+-e\d+(?:\.html)?/?$", re.I
)
# Le texte du lien educh accole au titre des métadonnées emoji :
# « Titre 📍 Lieu 🕒 Taux 📄 Contrat Employeur ». SÉLECTEUR À AJUSTER SI BESOIN.
_EDUCH_EMOJI = "📍🕒📄💼🗓️"
_EDUCH_SEG_RE = re.compile(rf"([{_EDUCH_EMOJI}])\s*([^{_EDUCH_EMOJI}]*)")
_EDUCH_CONTRACT_RE = re.compile(
    r"^(CDI|CDD|Permanent|Temporaire|Stage|Auxiliaire|Mission|Apprentissage|"
    r"Fixe|Int[ée]rim|Temps\s+partiel|Temps\s+plein)\b\s*", re.I)
_EDUCH_CONTRACT_WORD_RE = re.compile(
    r"\b(CDI|CDD|Permanent|Temporaire|Stage|Auxiliaire|Mission|Apprentissage|"
    r"Fixe|Int[ée]rim|Temps\s+partiel|Temps\s+plein)\b", re.I)
_EDUCH_VIEWS_RE = re.compile(r"\b\d+\s+vues?\b", re.I)
_EDUCH_APPLICATIONS_RE = re.compile(r"\b\d+\s+candidatures?\b", re.I)

# Les listes educh préfixent chaque libellé d'une date relative ou absolue
# (« il y a 2 heures », « 11 juin »…) ; on la retire avant d'isoler le titre.
_EDUCH_MONTHS = ("janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
                 "septembre|octobre|novembre|décembre|decembre")
_EDUCH_DATE_PREFIX_RE = re.compile(
    rf"^\s*(?:il\s+y\s+a\s+\d+\s+(?:minute|heure|jour|semaine|mois|an|année)s?"
    rf"|aujourd['’]hui|hier"
    rf"|\d{{1,2}}\s+(?:{_EDUCH_MONTHS}))\s+",
    re.I)


def _clean_educh_text(text: str) -> str:
    text = _EDUCH_DATE_PREFIX_RE.sub("", str(text or "").strip())
    text = _EDUCH_VIEWS_RE.sub(" ", text)
    text = _EDUCH_APPLICATIONS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -–·|")


def _educh_location_match(text: str):
    for place in sorted(GEO_OK, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(place).replace(r'\ ', r'\s+')}\b", re.I)
        m = pattern.search(text)
        if m:
            return m
    return None


def _remove_text_part(text: str, part: str) -> str:
    if not part:
        return text
    return re.sub(re.escape(part), " ", text, count=1).strip()


def _trim_location_suffix(company: str, location: str) -> str:
    if not company or not location:
        return company
    return re.sub(rf"\s+{re.escape(location)}$", "", company, flags=re.I).strip()


def _parse_educh_plain_anchor(text: str):
    """Parse le format sans emojis : titre + métadonnées visibles en texte brut."""
    location = taux = company = ""
    loc_m = _educh_location_match(text)
    if loc_m:
        location = loc_m.group(0)
    taux = extract_taux(text)
    company = extract_employer(text)
    company = _EDUCH_CONTRACT_WORD_RE.sub(" ", company)
    company = re.sub(r"\s+", " ", company).strip(" -–·,|")
    company = _trim_location_suffix(company, location)

    title = text
    for part in (company, location, taux):
        title = _remove_text_part(title, part)
    title = _EDUCH_CONTRACT_WORD_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -–·,|")
    return title or text, location, taux, company


def _parse_educh_anchor(text: str):
    """Décompose le libellé d'un lien educh en (titre, lieu, taux, employeur).

    L'employeur (segment 📄, après le type de contrat) sert de `company` réelle :
    cela évite l'enrichissement par lecture de page (la page educh est polluée par
    d'autres annonces, ce qui fausserait la pertinence). Champs absents → "".
    """
    text = _clean_educh_text(text)
    if not any(marker in text for marker in _EDUCH_EMOJI):
        return _parse_educh_plain_anchor(text)
    title = re.split(rf"[{_EDUCH_EMOJI}]", text, maxsplit=1)[0].strip(" -–·|")
    location = taux = company = ""
    for marker, val in _EDUCH_SEG_RE.findall(text):
        val = val.strip()
        if marker == "📍":
            location = val
        elif marker == "🕒":
            taux = val
        elif marker == "📄":
            company = _EDUCH_CONTRACT_RE.sub("", val).strip(" -–·,")
    return title, location, taux, company


def _parse_educh_page(html: str, base: str = "https://www.educh.ch") -> list:
    """Parse les liens d'offres educh depuis une fixture ou une page réelle."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not _EDUCH_OFFER_RE.fullmatch(urlparse(href).path):
            continue
        full_url = urljoin(base + "/", href)
        url_key = canonical_url(full_url)
        if url_key in seen:
            continue
        title, location, taux, company = _parse_educh_anchor(
            anchor.get_text(" ", strip=True)
        )
        seen.add(url_key)
        offers.append({
            "title": title,
            "url": full_url,
            "location": location or "Genève",
            "taux": taux,
            "company": company or "—",
        })
    return offers


def scrape_educh() -> list:
    """Offres educh.ch (petite enfance, social, enseignement spécialisé) — Genève.

    Le robots.txt d'educh.ch autorise le crawl (User-agent: * sans Disallow, et un
    sitemap d'offres dédié). On lit directement la liste DÉJÀ filtrée par canton
    pour ne garder que Genève sans parcourir les ~300 offres nationales.
    """
    jobs, seen_urls = [], set()
    raw_links = 0
    mark_raw_source("educh.ch")
    # Source principale : la page de recherche Genève (fonctionne, déjà ciblée).
    # Les listings /emploi/<canton>/ ont déjà renvoyé des erreurs serveur ; on les
    # garde tant qu'ils répondent, car seen_urls dédoublonne et in_zone() assure le
    # filtre géographique.
    for url in ("https://www.educh.ch/recherche/geneve.html",
                "https://www.educh.ch/emploi/geneve-canton/",
                "https://www.educh.ch/emploi/geneve-ville/"):
        soup = fetch(url)
        if not soup:
            continue
        for offer in _parse_educh_page(str(soup), "https://www.educh.ch"):
            raw_links += 1
            fields = {
                "company": offer["company"],
                "source": "educh.ch",
                "location": offer["location"],
            }
            if offer["taux"]:
                fields["taux"] = offer["taux"]
            consider(
                offer["title"], offer["url"], fields, jobs, seen_urls
            )
    if raw_links == 0:
        log("⚠️  educh.ch: 0 lien d'offre extrait — sélecteur potentiellement cassé")
    log(f"educh.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_bibliosuisse() -> list:
    """Offres bibliothécaire/archiviste/documentaliste — Bibliosuisse.

    Board national de l'association suisse des bibliothèques. La plupart des
    annonces sont alémaniques (écartées par la langue/les mots-clés français) ;
    on conserve les romandes/genevoises. robots.txt : autorisé.

    NB : le sélecteur .articlewrapper fonctionne (vérifié : il extrait bien les
    offres présentes, ex. Zoug/Berne). Un total de 0 = aucune offre romande/FR
    ce jour-là, PAS un sélecteur cassé — le board est presque toujours 100 %
    alémanique. D'où l'inscription dans HEALTH_SILENT_SOURCES (pas de fausse
    alerte « cassé »).
    """
    jobs, seen_urls = [], set()
    url = "https://www.bibliosuisse.ch/fr/shop/offres-demploi"
    soup = fetch(url)
    if not soup:
        return jobs
    # SÉLECTEUR À AJUSTER SI BESOIN : chaque offre est un bloc .articlewrapper.
    wraps = soup.select("div.articlewrapper")
    for wrap in wraps:
        a = wrap.find("a", href=True)
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if href and not href.startswith("http"):
            href = urljoin(url, href)
        consider(title, href,
                 {"company": "", "source": "bibliosuisse.ch", "location": ""},
                 jobs, seen_urls)
    if not wraps:
        log("⚠️  bibliosuisse.ch: 0 bloc d'offre — sélecteur potentiellement cassé")
    log(f"bibliosuisse.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Sources complémentaires du profil Systèmes & Linux
# ---------------------------------------------------------------------------

def _job_anchor_title(anchor) -> str:
    """Titre court d'une carte d'emploi, même si le lien recouvre toute la carte."""
    heading = anchor.select_one("h1, h2, h3, h4, [class*='title'], [class*='Title']")
    raw = heading.get_text(" ", strip=True) if heading else anchor.get_text(" ", strip=True)
    if not raw or normalize(raw) in {
        "voir l'offre", "view job", "view more", "read more", "learn more",
        "details", "en savoir plus", "postuler", "apply",
    }:
        node = anchor
        for _ in range(6):
            node = getattr(node, "parent", None)
            if node is None or len(node.get_text(" ", strip=True)) > 1800:
                break
            heading = node.select_one("h1, h2, h3, h4, [class*='title'], [class*='Title']")
            if heading:
                candidate = heading.get_text(" ", strip=True)
                if candidate:
                    raw = candidate
                    break
    return re.sub(r"\s+", " ", raw).strip()


def _job_card(anchor, max_depth: int = 7):
    """Remonte au plus petit conteneur de carte qui porte des métadonnées utiles."""
    node = anchor
    best = anchor
    for _ in range(max_depth):
        parent = getattr(node, "parent", None)
        if parent is None:
            break
        text = parent.get_text(" ", strip=True)
        if len(text) > 1800:
            break
        best = parent
        node = parent
        if getattr(parent, "name", "") in ("article", "li", "tr"):
            break
    return best


def _job_card_in_target_zone(anchor):
    """Carte et texte si un lieu Genève/Nyon y est explicitement présent."""
    node = anchor
    for _ in range(8):
        node = getattr(node, "parent", None)
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if len(text) > 2200:
            break
        if term_in(normalize(text), _GEO_OK_RE):
            return node, text
        if getattr(node, "name", "") in ("article", "li", "tr"):
            break
    return None, ""


def _company_from_card(card, fallback: str = "—") -> str:
    if card is None:
        return fallback
    el = card.select_one(
        "[itemprop='hiringOrganization'], [class*='company'], [class*='Company'], "
        "[class*='employer'], [class*='Employer']"
    )
    company = el.get_text(" ", strip=True) if el else ""
    return company[:120] if company else fallback


def scrape_swissdevjobs() -> list:
    """SwissDevJobs — flux public léger, filtré localement sur Genève/Nyon."""
    if ACTIVE_PROFILE != "systemes":
        return []
    API_URL = "https://swissdevjobs.ch/api/jobsLight"
    SOURCE = "swissdevjobs.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not robots_allows(API_URL):
        return jobs
    try:
        _polite_wait(API_URL)
        response = session().get(
            API_URL, headers={**HEADERS, "Accept": "application/json"}, timeout=25
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        log(f"Erreur SwissDevJobs API: {exc}")
        return jobs
    items = payload if isinstance(payload, list) else payload.get("jobs", [])
    for item in items:
        if not isinstance(item, dict):
            continue
        record_raw_candidate(SOURCE)
        title = str(item.get("name") or item.get("title") or "").strip()
        company = str(item.get("company") or item.get("companyName") or "—").strip()
        location = str(
            item.get("city") or item.get("location") or item.get("cityCategory") or ""
        ).strip()
        if structured_geography(location)["status"] != "target":
            continue
        job_url = str(
            item.get("jobUrl") or item.get("url") or item.get("slug") or ""
        ).strip()
        if not job_url:
            continue
        full_url = urljoin("https://swissdevjobs.ch/jobs/", job_url)
        consider(
            title, full_url,
            {"company": company, "source": SOURCE, "location": location,
             "external_id": str(item.get("_id") or item.get("id") or ""),
             "_raw_recorded": True},
            jobs, seen_urls,
        )
    log(f"SwissDevJobs: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_itjobs_ch() -> list:
    """itjobs.ch — catégories System Engineering et System Administration."""
    if ACTIVE_PROFILE != "systemes":
        return []
    URLS = (
        "https://www.itjobs.ch/jobs",
        "https://www.itjobs.ch/jobs/system-engineering",
        "https://www.itjobs.ch/jobs/system-administration",
    )
    DETAIL_RE = re.compile(r"^/jobs/\d+-")
    SOURCE = "itjobs.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    for url in URLS:
        soup = fetch(url)
        if not soup:
            continue
        for a in soup.select("a[href*='/jobs/']"):
            full_url = urljoin(url, a.get("href", ""))
            if not DETAIL_RE.match(urlparse(full_url).path):
                continue
            card, card_text = _job_card_in_target_zone(a)
            if card is None:              # board national : lieu explicite obligatoire
                continue
            title = _job_anchor_title(a)
            consider(title, full_url,
                     {"company": _company_from_card(card), "source": SOURCE,
                      "location": card_text[:160]}, jobs, seen_urls)
    log(f"itjobs.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_itboard() -> list:
    """ITBoard — board IT suisse, filtré strictement sur Genève/Nyon."""
    if ACTIVE_PROFILE != "systemes":
        return []
    LIST_URL = "https://www.itboard.ch/"
    DETAIL_RE = re.compile(r"^/job/[^/]+/?$")
    SOURCE = "itboard.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    for a in soup.select("a[href^='/job/'], a[href*='itboard.ch/job/']"):
        full_url = urljoin(LIST_URL, a.get("href", ""))
        if not DETAIL_RE.match(urlparse(full_url).path):
            continue
        card, card_text = _job_card_in_target_zone(a)
        if card is None:
            continue
        title = _job_anchor_title(a)
        consider(title, full_url,
                 {"company": _company_from_card(card), "source": SOURCE,
                  "location": card_text[:160]}, jobs, seen_urls)
    log(f"ITBoard: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_cern() -> list:
    """CERN Careers — offres du site de Meyrin/Genève."""
    LIST_URL = "https://careers.cern/jobs/"
    DETAIL_RE = re.compile(r"^/jobs/[^/]+/?$")
    SOURCE = "careers.cern"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    for a in soup.select("a[href*='/jobs/']"):
        full_url = urljoin(LIST_URL, a.get("href", ""))
        if not DETAIL_RE.match(urlparse(full_url).path):
            continue
        title = _job_anchor_title(a)
        consider(title, full_url,
                 {"company": "CERN", "source": SOURCE, "location": "Genève"},
                 jobs, seen_urls)
    log(f"CERN Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_icrc() -> list:
    """CICR — opportunités du siège à Genève (SAP SuccessFactors)."""
    LIST_URL = "https://careers.icrc.org/go/HQ-Opportunities/8821401/"
    DETAIL_RE = re.compile(r"/job/")
    SOURCE = "careers.icrc.org"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    for a in soup.select("a[href*='/job/']"):
        full_url = urljoin(LIST_URL, a.get("href", ""))
        if not DETAIL_RE.search(urlparse(full_url).path):
            continue
        title = _job_anchor_title(a)
        consider(title, full_url,
                 {"company": "CICR", "source": SOURCE, "location": "Genève"},
                 jobs, seen_urls)
    log(f"CICR Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


_WIPO_SWISS_RE = _compile_terms(
    ("switzerland", "suisse", "schweiz"), inflect=False
)


def _parse_wipo_listing(html: str, base_url: str) -> list:
    """Extrait les fiches de la liste unifiée Taleo, sans accès réseau."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.select("a[href*='jobdetail.ftl'][href*='job=']"):
        title = _job_anchor_title(anchor)
        url = urljoin(base_url, anchor.get("href", ""))
        key = canonical_url(url)
        if not title or key in seen:
            continue
        location = ""
        node = anchor
        for _ in range(6):
            node = getattr(node, "parent", None)
            if node is None:
                break
            context = node.get_text(" ", strip=True)
            if len(context) > 1000:
                break
            context_norm = normalize(context)
            context_geo = structured_geography(context)
            if context_geo["status"] == "target":
                location = context_geo["evidence"].title()
                break
            if context_geo["status"] == "outside":
                location = context_geo["evidence"]
                break
            if term_in(context_norm, _WIPO_SWISS_RE):
                # Le site suisse de WIPO correspond à son siège genevois ; les
                # bureaux extérieurs sont, eux, nommés par pays/ville.
                location = "Genève"
                break
        seen.add(key)
        offers.append({"title": title, "url": url, "location": location})
    return offers


def scrape_wipo() -> list:
    """OMPI/WIPO — sections officielles Taleo (personnel et affiliations)."""
    LIST_URLS = (
        "https://wipo.taleo.net/careersection/wp_2/moresearch.ftl?lang=en",
        "https://wipo.taleo.net/careersection/wp_1/moresearch.ftl?lang=en",
        "https://wipo.taleo.net/careersection/wp_internship/moresearch.ftl?lang=en",
        "https://wipo.taleo.net/careersection/wp_fellowship/moresearch.ftl?lang=en",
    )
    SOURCE = "wipo.taleo.net"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    for list_url in LIST_URLS:
        soup = fetch(list_url)
        if not soup:
            continue
        for offer in _parse_wipo_listing(str(soup), list_url):
            # La liste WIPO contient aussi des bureaux extérieurs. On n'invente
            # donc pas Genève : l'enrichissement lit le « Duty Station » de la
            # fiche, puis la géographie structurée tranche.
            consider(
                offer["title"], offer["url"],
                {"company": "OMPI / WIPO", "source": SOURCE,
                 "location": offer["location"]},
                jobs, seen_urls,
            )
    log(f"WIPO Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_job_room() -> list:
    """Job-Room — recherche publique Systèmes à Genève via son interface JS."""
    if ACTIVE_PROFILE != "systemes":
        return []
    LIST_URL = "https://www.job-room.ch/home/latest/index.html"
    SOURCE = "job-room.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not PLAYWRIGHT_AVAILABLE:
        log("Job-Room : Playwright non installé — source ignorée")
        return jobs
    if not robots_allows(LIST_URL):
        return jobs
    try:
        with _sync_playwright() as pw:
            browser = _launch_chromium(pw)
            ctx = _new_stealth_context(browser)
            page = ctx.new_page()
            try:
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2500)
                keyword = page.get_by_label(
                    re.compile(r"Keywords|Mots-cl[ée]s|comp[ée]tences", re.I))
                if not keyword.count():
                    keyword = page.get_by_placeholder(
                        re.compile(r"Keywords|Mots-cl[ée]s|profession|emploi", re.I)
                    )
                location = page.get_by_label(
                    re.compile(r"Canton|Work location|Lieu de travail", re.I))
                if keyword.count():
                    keyword.first.fill("system engineer linux")
                    keyword.first.press("Enter")
                else:
                    log("Erreur Job-Room : champ de recherche introuvable")
                if location.count():
                    location.first.fill("Genève")
                    page.wait_for_timeout(800)
                    location.first.press("ArrowDown")
                    location.first.press("Enter")
                search = page.get_by_role(
                    "button", name=re.compile(r"Search job|Rechercher", re.I))
                if search.count():
                    search.first.click()
                    page.wait_for_timeout(3500)
                soup = BeautifulSoup(page.content(), "lxml")
            finally:
                browser.close()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not any(part in href for part in
                       ("job-search", "job-detail", "vacancy", "/job/", "/jobs/")):
                continue
            title = _job_anchor_title(a)
            if not is_relevant(title):
                continue
            card, card_text = _job_card_in_target_zone(a)
            if card is None:
                continue
            consider(title, urljoin(LIST_URL, href),
                     {"company": _company_from_card(card), "source": SOURCE,
                      "location": card_text[:160]}, jobs, seen_urls)
    except PlaywrightBrowserUnavailable:
        raise
    except Exception as e:
        _log_playwright_failure(LIST_URL, e)
    log(f"Job-Room: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_sig() -> list:
    """Services industriels de Genève — listing SuccessFactors public."""
    URLS = (
        "https://jobs.sig-ge.ch/go/Nos-offres-d%27emploi/4179501/",
        ("https://jobs.sig-ge.ch/go/Nos-offres-d%26apos%3Bemploi/4179501/10/"
         "?q=&sortColumn=referencedate&sortDirection=desc"),
    )
    SOURCE = "jobs.sig-ge.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    for url in URLS:
        soup = fetch(url)
        if not soup:
            continue
        for a in soup.select("a[href*='/job/']"):
            full_url = urljoin(url, a.get("href", ""))
            title = _job_anchor_title(a)
            consider(title, full_url,
                     {"company": "Services industriels de Genève", "source": SOURCE,
                      "location": "Genève"}, jobs, seen_urls)
    log(f"SIG Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_tpg() -> list:
    """Transports publics genevois — portail SuccessFactors rendu en JavaScript."""
    LIST_URL = (
        "https://career5.successfactors.eu/career?company=transpor01"
        "&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH"
    )
    SELECTOR = (
        "a[href*='jobId='], a[href*='jobReqId='], "
        "a[href*='career_job_req_id='], a[href*='/job/']"
    )
    SOURCE = "tpg.ch"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not robots_allows(LIST_URL):
        return jobs
    soup = fetch_via_playwright(LIST_URL, wait_selector=SELECTOR)
    if not soup:
        return jobs
    for a in soup.select(SELECTOR):
        title = _job_anchor_title(a)
        full_url = urljoin(LIST_URL, a.get("href", ""))
        consider(title, full_url,
                 {"company": "Transports publics genevois", "source": SOURCE,
                  "location": "Genève"}, jobs, seen_urls)
    log(f"TPG Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _parse_un_job_feed(html: str, base_url: str) -> list:
    """Extrait les fiches structurées du flux public UN Careers."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.select("a[href*='/jobSearchDescription/']"):
        full_url = urljoin(base_url, anchor.get("href", ""))
        match = re.search(r"/jobSearchDescription/(\d+)", urlparse(full_url).path)
        if not match or match.group(1) in seen:
            continue
        title = _job_anchor_title(anchor)
        node, context = anchor, title
        for _ in range(8):
            parent = getattr(node, "parent", None)
            if parent is None:
                break
            parent_text = parent.get_text(" ", strip=True)
            if len(parent_text) > 3500:
                break
            node, context = parent, parent_text
            if re.search(r"Duty Station\s*:", context, re.I):
                break
        duty = re.search(
            r"Duty Station\s*:\s*(.+?)(?=\s+(?:Staffing Exercise|Date Posted|Deadline|$))",
            context, re.I,
        )
        office = re.search(
            r"Department/Office\s*:\s*(.+?)(?=\s+Duty Station\s*:)",
            context, re.I,
        )
        seen.add(match.group(1))
        offers.append({
            "title": title, "url": full_url, "external_id": match.group(1),
            "location": duty.group(1).strip() if duty else "",
            "company": office.group(1).strip() if office else "Organisation des Nations Unies",
            "description": context[:2500],
        })
    return offers


def scrape_un_geneva() -> list:
    """ONU Genève — flux public structuré, avec repli navigateur."""
    LIST_URL = "https://careers.un.org/jobfeed?isPage=true&language=en"
    SELECTOR = "a[href*='/jobSearchDescription/']"
    SOURCE = "careers.un.org"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not robots_allows(LIST_URL):
        return jobs
    soup = fetch(LIST_URL)
    if not soup or not soup.select_one(SELECTOR):
        soup = fetch_via_playwright(LIST_URL, wait_selector=SELECTOR)
    if not soup:
        return jobs
    for offer in _parse_un_job_feed(str(soup), LIST_URL):
        if structured_geography(offer["location"])["status"] != "target":
            continue
        consider(
            offer["title"], offer["url"],
            {"company": offer["company"], "source": SOURCE,
             "location": offer["location"], "description": offer["description"],
             "external_id": offer["external_id"]},
            jobs, seen_urls,
        )
    log(f"ONU Genève: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_wto() -> list:
    """OMC/WTO — API JSON publique utilisée par le portail Workday."""
    API_URL = "https://wto.wd103.myworkdayjobs.com/wday/cxs/wto/External/jobs"
    PUBLIC_BASE = "https://wto.wd103.myworkdayjobs.com/en-US/External"
    SOURCE = "wto.org"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not robots_allows(API_URL):
        return jobs
    try:
        for item in _workday_job_postings(API_URL):
            title = str(item.get("title", "")).strip()
            path = item.get("externalPath", "")
            if not title or not path:
                continue
            consider(title, urljoin(PUBLIC_BASE + "/", path.lstrip("/")),
                     {"company": "OMC / WTO", "source": SOURCE,
                      "location": "Genève"}, jobs, seen_urls)
    except Exception as e:
        log(f"WTO Workday: {e}")
    log(f"WTO Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _workday_job_postings(api_url: str, page_size: int = 20,
                          max_results: int = 500) -> list:
    """Lit un portail Workday CXS avec sa taille de page réellement acceptée.

    Les endpoints CXS actuellement utilisés par WTO et Lombard Odier refusent
    les requêtes à 100 éléments avec HTTP 400. Le client web Workday pagine par
    blocs de 20 ; on suit ce contrat et on s'arrête au total annoncé.
    """
    page_size = max(1, min(int(page_size), 20))
    postings = []
    for offset in range(0, max_results, page_size):
        _polite_wait(api_url)
        response = session().post(
            api_url,
            json={"appliedFacets": {}, "limit": page_size, "offset": offset,
                  "searchText": ""},
            headers={**HEADERS, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("jobPostings", [])
        postings.extend(page)
        total = int(payload.get("total", len(postings)) or 0)
        if len(page) < page_size or len(postings) >= total:
            break
    return postings


def _platform_source_terms(source: str, default_terms: list) -> list:
    """Termes courts pour plateformes internationales, adaptés au profil actif."""
    return source_terms(source, default_terms)


def _reliefweb_items(query: str) -> list:
    api_url = f"https://api.reliefweb.int/v2/jobs?appname={quote(RELIEFWEB_APPNAME)}"
    payload = {
        "profile": "list",
        "preset": "latest",
        "limit": 50,
        "query": {"value": query, "operator": "AND"},
        "filter": {
            "conditions": [
                {"field": "country", "value": "Switzerland"},
            ]
        },
        "fields": {
            "include": [
                "title", "url", "body", "source", "country", "city", "date",
            ]
        },
    }
    _polite_wait(api_url)
    response = session().post(
        api_url,
        json=payload,
        headers={**HEADERS, "Accept": "application/json",
                 "Content-Type": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def _reliefweb_names(value) -> list:
    if isinstance(value, list):
        return [
            str(item.get("name", "") if isinstance(item, dict) else item).strip()
            for item in value
            if str(item.get("name", "") if isinstance(item, dict) else item).strip()
        ]
    if isinstance(value, dict):
        name = str(value.get("name", "")).strip()
        return [name] if name else []
    text = str(value or "").strip()
    return [text] if text else []


def scrape_reliefweb() -> list:
    """ReliefWeb Jobs — ONG/organisations internationales avec pays Suisse."""
    SOURCE = "reliefweb.int"
    default_terms = [
        "communication", "communications", "editor", "editorial",
        "knowledge management", "information management", "records management",
        "publishing", "translation", "social media",
    ]
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    if not RELIEFWEB_APPNAME:
        log(
            "ReliefWeb : RELIEFWEB_APPNAME pré-approuvé absent — "
            "source ignorée"
        )
        return jobs
    for term in _platform_source_terms("reliefweb", default_terms):
        mark_query(SOURCE, term)
        try:
            for item in _reliefweb_items(term):
                fields = item.get("fields", {})
                title = str(fields.get("title", "")).strip()
                url = str(fields.get("url", "")).strip()
                if not title or not url:
                    continue
                cities = _reliefweb_names(fields.get("city"))
                countries = _reliefweb_names(fields.get("country"))
                sources = _reliefweb_names(fields.get("source"))
                location = ", ".join(cities + countries) or "Suisse"
                company = sources[0] if sources else "ReliefWeb"
                consider(
                    title, url,
                    {"company": company, "source": SOURCE, "location": location,
                     "description": fields.get("body", ""), "_query": term},
                    jobs, seen_urls,
                )
        except Exception as e:
            log(f"ReliefWeb [{term}]: {e}")
    log(f"ReliefWeb: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _generic_job_cards_from_links(soup: BeautifulSoup, base_url: str):
    """Itère sur des cartes d'emploi best-effort pour plateformes sans API stable."""
    seen = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href or href.startswith(("mailto:", "tel:", "#")):
            continue
        full_url = urljoin(base_url, href)
        haystack = normalize(" ".join((href, a.get_text(" ", strip=True))))
        if not any(token in haystack for token in (
            "job", "jobs", "career", "careers", "vacancy", "vacancies",
            "emploi", "offre", "poste", "recruit",
        )):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        card = _job_card(a)
        card_text = card.get_text(" ", strip=True) if card else a.get_text(" ", strip=True)
        title = _job_anchor_title(a)
        if not title or len(title) < 5 or len(title) > 180:
            continue
        if normalize(title) in {"jobs", "job", "careers", "career opportunities"}:
            continue
        yield title, full_url, card, card_text


def _parse_cagi_page(html: str, base_url: str) -> list:
    """Extrait uniquement les vraies fiches `/job/<slug>/` du listing CAGI."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.select("a[href]"):
        full_url = urljoin(base_url, anchor.get("href", ""))
        path = urlparse(full_url).path
        if not re.fullmatch(r"/job/[^/]+/?", path):
            continue
        title = _job_anchor_title(anchor)
        key = canonical_url(full_url)
        if not title or len(title) < 5 or key in seen:
            continue
        card = _job_card(anchor)
        card_text = card.get_text(" ", strip=True) if card else title
        location = extract_location_hint(card_text)
        seen.add(key)
        offers.append({
            "title": title,
            "url": full_url,
            "location": location,
            "company": (
                _company_from_card(card, "") or extract_employer(card_text) or "CAGI"
            ),
            "description": card_text[:1200],
        })
    return offers


def scrape_cagi() -> list:
    """CAGI Recruitment Platform — offres ONG/Genève internationale."""
    SOURCE = "jobs.cagi.ch"
    # La page d'accueil `/` déclenche un mur anti-bot et maintient des connexions
    # qui empêchent `networkidle`. Ce listing officiel localisé est rendu côté
    # serveur et expose directement les fiches courantes.
    LIST_URL = "https://jobs.cagi.ch/fr/offres-demploi/"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    first_page = fetch(LIST_URL)
    if not first_page:
        first_page = fetch_via_playwright(
            LIST_URL, wait_selector="a[href*='/job/']"
        )
    if not first_page:
        return jobs

    pages = [(LIST_URL, first_page)]
    page_numbers = [
        int(match.group(1))
        for anchor in first_page.select("a[href]")
        if (match := re.search(
            r"/fr/offres-demploi/page/(\d+)/?",
            urlparse(urljoin(LIST_URL, anchor.get("href", ""))).path,
        ))
    ]
    last_page = min(max(page_numbers, default=1), 5)
    for number in range(2, last_page + 1):
        page_url = f"{LIST_URL}page/{number}/"
        soup = fetch(page_url)
        if soup:
            pages.append((page_url, soup))

    for page_url, soup in pages:
        for offer in _parse_cagi_page(str(soup), page_url):
            consider(
                offer["title"], offer["url"],
                {"company": offer["company"], "source": SOURCE,
                 "location": offer["location"],
                 "description": offer["description"]},
                jobs, seen_urls,
            )
    log(f"CAGI: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_cinfo() -> list:
    """cinfoPoste — portail suisse de la coopération internationale."""
    SOURCE = "cinfo.ch"
    LIST_URLS = (
        "https://www.cinfo.ch/en/jobs",
        "https://www.cinfo.ch/fr/jobs",
    )
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    for list_url in LIST_URLS:
        soup = fetch(list_url)
        if not soup:
            continue
        for title, full_url, card, card_text in _generic_job_cards_from_links(soup, list_url):
            location = extract_location_hint(card_text)
            if not location and term_in(normalize(card_text), _GEO_OK_RE):
                location = "Genève"
            company = _company_from_card(card, "") or extract_employer(card_text) or "cinfo"
            consider(
                title, full_url,
                {"company": company, "source": SOURCE, "location": location or "Suisse",
                 "description": card_text[:1200]},
                jobs, seen_urls,
            )
    log(f"cinfoPoste: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _parse_heading_job_cards(html: str, base_url: str) -> list:
    """Parse les listings institutionnels organisés par titres et cartes."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    noise = {
        "offres d emploi", "current vacancies", "job opportunities",
        "offres d'emploi", "explore our current job opportunities",
    }
    for heading in soup.select("h2, h3, h4, h5"):
        title = clean_job_title(heading.get_text(" ", strip=True))
        if not title or len(title) < 5 or len(title) > 240 or normalize(title) in noise:
            continue
        node, context, link = heading, title, heading.select_one("a[href]")
        for _ in range(6):
            parent = getattr(node, "parent", None)
            if parent is None:
                break
            text_value = parent.get_text(" ", strip=True)
            if len(text_value) > 3000:
                break
            node, context = parent, text_value
            candidates = parent.select("a[href]")
            preferred = next((a for a in candidates if normalize(a.get_text(" ", strip=True))
                              in {"view more", "en savoir plus", "voir l annonce",
                                  "voir l'annonce", "apply", "postuler"}), None)
            link = preferred or link or (candidates[0] if candidates else None)
            if getattr(parent, "name", "") in ("article", "li"):
                break
        if link is None:
            continue
        full_url = urljoin(base_url, link.get("href", ""))
        if not full_url or urlparse(full_url).scheme not in ("http", "https"):
            continue
        key = canonical_url(full_url)
        if key == canonical_url(base_url) or key in seen:
            continue
        location = extract_location_hint(context)
        if not location:
            geo = structured_geography(context)
            if geo["status"] != "unknown":
                location = display_location(geo["evidence"].title())
        seen.add(key)
        offers.append({
            "title": title, "url": full_url, "location": location,
            "description": context[:2500],
        })
    return offers


def scrape_ecolint() -> list:
    """École internationale de Genève — postes pédagogiques et supports."""
    SOURCE = "ecolint.ch"
    LIST_URL = "https://www.ecolint.ch/fr/emploi"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    for offer in _parse_heading_job_cards(str(soup), LIST_URL):
        location = offer["location"] or "Genève / Founex"
        consider(
            offer["title"], offer["url"],
            {"company": "École internationale de Genève", "source": SOURCE,
             "location": location, "description": offer["description"]},
            jobs, seen_urls,
        )
    log(f"Ecolint: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_ville_nyon() -> list:
    """Ville de Nyon — page officielle des postes vacants."""
    SOURCE = "nyon.ch"
    LIST_URL = (
        "https://www.nyon.ch/vivre-a-nyon/economie-emploi-formation/"
        "travailler-a-la-ville-de-nyon/offres-d-emploi-1578"
    )
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    offers = _parse_heading_job_cards(str(soup), LIST_URL)
    for title, full_url, _card, card_text in _generic_job_cards_from_links(soup, LIST_URL):
        if urlparse(full_url).netloc.endswith(("jobup.ch", "nyon.ch")):
            offers.append({
                "title": title, "url": full_url, "location": "Nyon",
                "description": card_text[:1800],
            })
    for offer in offers:
        consider(
            offer["title"], offer["url"],
            {"company": "Ville de Nyon", "source": SOURCE,
             "location": "Nyon", "description": offer["description"]},
            jobs, seen_urls,
        )
    log(f"Ville de Nyon: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_unicef() -> list:
    """UNICEF — listing PageUp préfiltré sur la Suisse."""
    SOURCE = "jobs.unicef.org"
    LIST_URL = "https://jobs.unicef.org/en-us/Search/?location=switzerland"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup:
        return jobs
    for offer in _parse_heading_job_cards(str(soup), LIST_URL):
        # Le filtre Suisse couvre aussi d'autres villes : Genève/Nyon doit être
        # explicite, sinon le funnel envoie seulement les métiers forts en revue.
        consider(
            offer["title"], offer["url"],
            {"company": "UNICEF", "source": SOURCE,
             "location": offer["location"], "description": offer["description"]},
            jobs, seen_urls,
        )
    log(f"UNICEF Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _parse_taleo_job_listing(html: str, base_url: str) -> list:
    """Parseur Taleo générique sans supposer que tout poste suisse est genevois."""
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.select("a[href*='jobdetail.ftl'][href*='job=']"):
        full_url = urljoin(base_url, anchor.get("href", ""))
        key = canonical_url(full_url)
        title = _job_anchor_title(anchor)
        if not title or key in seen:
            continue
        card = _job_card(anchor)
        context = card.get_text(" ", strip=True) if card else title
        location = extract_location_hint(context)
        if not location:
            geo = structured_geography(context)
            if geo["status"] != "unknown":
                location = geo["evidence"]
        job_id_match = re.search(r"[?&]job=([^&#]+)", full_url)
        seen.add(key)
        offers.append({
            "title": title, "url": full_url, "location": location,
            "description": context[:2000],
            "external_id": job_id_match.group(1) if job_id_match else "",
        })
    return offers


def scrape_who() -> list:
    """OMS/WHO — portail Taleo externe."""
    SOURCE = "careers.who.int"
    LIST_URL = "https://careers.who.int/careersection/ex/moresearch.ftl?lang=en"
    SELECTOR = "a[href*='jobdetail.ftl'][href*='job=']"
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch(LIST_URL)
    if not soup or not soup.select_one(SELECTOR):
        soup = fetch_via_playwright(LIST_URL, wait_selector=SELECTOR)
    if not soup:
        return jobs
    for offer in _parse_taleo_job_listing(str(soup), LIST_URL):
        consider(
            offer["title"], offer["url"],
            {"company": "Organisation mondiale de la Santé", "source": SOURCE,
             "location": offer["location"], "description": offer["description"],
             "external_id": offer["external_id"]},
            jobs, seen_urls,
        )
    log(f"WHO Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def scrape_pictet() -> list:
    """Pictet — ancien portail SuccessFactors rendu côté client."""
    SOURCE = "careers.pictet.com"
    LIST_URL = (
        "https://career012.successfactors.eu/career?company=banquepict"
        "&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH"
    )
    SELECTOR = (
        "a[href*='jobId='], a[href*='jobReqId='], "
        "a[href*='career_job_req_id='], a[href*='/job/']"
    )
    jobs, seen_urls = [], set()
    mark_raw_source(SOURCE)
    soup = fetch_via_playwright(LIST_URL, wait_selector=SELECTOR)
    if not soup:
        return jobs
    for anchor in soup.select(SELECTOR):
        title = _job_anchor_title(anchor)
        full_url = urljoin(LIST_URL, anchor.get("href", ""))
        card, card_text = _job_card_in_target_zone(anchor)
        if card is None:
            continue
        consider(
            title, full_url,
            {"company": "Pictet", "source": SOURCE,
             "location": extract_location_hint(card_text) or "Genève",
             "description": card_text[:1800]},
            jobs, seen_urls,
        )
    log(f"Pictet Careers: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Alternatives conformes au scraping LinkedIn
# ---------------------------------------------------------------------------

_LINKEDIN_JOB_RE = re.compile(
    r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", re.IGNORECASE
)
_LINKEDIN_EMAIL_NOISE = re.compile(
    r"^(?:voir l['’]offre|view job|postuler|apply|emplois? similaires?|"
    r"similar jobs?|linkedin|se désabonner|unsubscribe)$",
    re.IGNORECASE,
)


def _canonical_linkedin_job_url(url: str) -> str:
    """Retourne seulement l'URL canonique d'une offre LinkedIn identifiée."""
    parsed = urlparse(str(url or ""))
    if not parsed.netloc.lower().endswith("linkedin.com"):
        return ""
    match = _LINKEDIN_JOB_RE.search(parsed.path)
    if not match:
        return ""
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"


def _linkedin_email_parts(message) -> tuple[str, str]:
    html_parts, text_parts = [], []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        (html_parts if content_type == "text/html" else text_parts).append(str(content))
    return "\n".join(html_parts), "\n".join(text_parts)


def _linkedin_anchor_context(anchor) -> tuple[str, str]:
    """Déduit employeur et lieu dans le petit bloc HTML entourant une offre."""
    best_lines = []
    node = anchor
    for _ in range(7):
        node = node.parent
        if node is None:
            break
        lines = [re.sub(r"\s+", " ", value).strip()
                 for value in node.stripped_strings]
        lines = list(dict.fromkeys(value for value in lines if value))
        if 2 <= len(lines) <= 12 and sum(map(len, lines)) <= 1000:
            best_lines = lines
        if any(term_in(normalize(value), _GEO_OK_RE) for value in lines):
            best_lines = lines
            break

    title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
    candidates = [line for line in best_lines
                  if normalize(line) != normalize(title)
                  and not _LINKEDIN_EMAIL_NOISE.match(line)]
    location = next(
        (line for line in candidates if term_in(normalize(line), _GEO_OK_RE)), ""
    )
    company = next(
        (line for line in candidates if line != location and len(line) <= 120), "—"
    )
    return company, location


def parse_linkedin_alert_message(raw_message: bytes) -> list:
    """Extrait les offres d'une alerte email, sans requête vers LinkedIn."""
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = str(message.get("From", ""))
    subject = str(message.get("Subject", ""))
    if "linkedin" not in normalize(sender):
        return []
    if not re.search(r"job|emploi|offre|alerte|alert", normalize(subject)):
        return []

    html, _ = _linkedin_email_parts(message)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    offers, seen = [], set()
    for anchor in soup.select("a[href]"):
        canonical = _canonical_linkedin_job_url(anchor.get("href", ""))
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not canonical or canonical in seen or len(title) < 4:
            continue
        if _LINKEDIN_EMAIL_NOISE.match(title):
            continue
        company, location = _linkedin_anchor_context(anchor)
        offers.append({
            "title": title,
            "company": company,
            "location": location or LINKEDIN_ALERT_DEFAULT_LOCATION,
            "url": canonical,
        })
        seen.add(canonical)
    return offers


def scrape_linkedin_alert_emails() -> list:
    """Lit en IMAP les alertes LinkedIn, sans marquer les messages comme lus."""
    source = "LinkedIn (alerte email)"
    jobs, seen_urls = [], set()
    mark_raw_source(source)
    if not all((LINKEDIN_IMAP_HOST, LINKEDIN_IMAP_USER, LINKEDIN_IMAP_PASS)):
        log("Alertes LinkedIn par email : configuration absente — source ignorée")
        return jobs
    since = (local_now() - timedelta(days=max(1, LINKEDIN_IMAP_DAYS))).strftime(
        "%d-%b-%Y"
    )
    try:
        with imaplib.IMAP4_SSL(LINKEDIN_IMAP_HOST, LINKEDIN_IMAP_PORT) as mailbox:
            mailbox.login(LINKEDIN_IMAP_USER, LINKEDIN_IMAP_PASS)
            status, _ = mailbox.select(LINKEDIN_IMAP_FOLDER, readonly=True)
            if status != "OK":
                raise RuntimeError(f"dossier IMAP inaccessible: {LINKEDIN_IMAP_FOLDER}")
            status, payload = mailbox.search(None, "SINCE", since)
            if status != "OK":
                raise RuntimeError("recherche IMAP refusée")
            message_ids = payload[0].split()[-LINKEDIN_IMAP_MAX_MESSAGES:]
            for message_id in reversed(message_ids):
                status, chunks = mailbox.fetch(message_id, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next(
                    (chunk[1] for chunk in chunks
                     if isinstance(chunk, tuple) and isinstance(chunk[1], bytes)),
                    b"",
                )
                for offer in parse_linkedin_alert_message(raw):
                    consider(
                        offer["title"], offer["url"],
                        {
                            "company": offer["company"],
                            "source": source,
                            "location": offer["location"],
                            # Interdiction explicite de télécharger la fiche.
                            "_no_fetch": True,
                        },
                        jobs, seen_urls,
                    )
    except Exception as exc:
        log(f"Alertes LinkedIn par email: {exc}")
    log(f"Alertes LinkedIn par email: {len(jobs)} offre(s) trouvée(s)")
    return jobs


def _load_ats_sources() -> list:
    if not ATS_SOURCES_FILE.exists():
        return []
    try:
        data = json.loads(ATS_SOURCES_FILE.read_text(encoding="utf-8"))
        return data.get("sources", []) if isinstance(data, dict) else []
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Configuration ATS invalide: {exc}")
        return []


def _scrape_workday_source(config: dict, jobs: list, seen_urls: set):
    host = config["host"].rstrip("/")
    tenant, site = config["tenant"], config["site"]
    api_url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    public_base = config.get("public_base", f"{host}/en-US/{site}").rstrip("/")
    for item in _workday_job_postings(
        api_url, page_size=config.get("page_size", 20)
    ):
        title, path = str(item.get("title", "")).strip(), item.get("externalPath", "")
        if not title or not path:
            continue
        consider(
            title, urljoin(public_base + "/", str(path).lstrip("/")),
            {"company": config["name"], "source": config["source"],
             "location": item.get("locationsText", ""),
             "external_id": str(path).rstrip("/").rsplit("/", 1)[-1],
             "_health_source": "Portails ATS directs"},
            jobs, seen_urls,
        )


def _smartrecruiters_location(value) -> str:
    if isinstance(value, dict):
        return ", ".join(
            str(value.get(key, "")).strip()
            for key in ("city", "region", "country") if value.get(key)
        )
    return str(value or "")


def _smartrecruiters_public_url(company_id: str, item: dict) -> str:
    """Construit la fiche publique (le champ `ref` peut pointer vers l'API)."""
    for key in ("postingUrl", "jobAdUrl"):
        candidate = str(item.get(key, ""))
        if urlparse(candidate).netloc.lower() == "jobs.smartrecruiters.com":
            return candidate
    title_slug = re.sub(r"[^a-z0-9]+", "-", normalize(item.get("name", ""))).strip("-")
    suffix = f"-{title_slug}" if title_slug else ""
    return (
        f"https://jobs.smartrecruiters.com/{company_id}/"
        f"{item.get('id', '')}{suffix}"
    )


def _scrape_smartrecruiters_source(config: dict, jobs: list, seen_urls: set):
    company_id = config["company_id"]
    api_url = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    offset = 0
    while offset < 500:
        _polite_wait(api_url)
        response = session().get(
            api_url, params={"limit": 100, "offset": offset},
            headers={**HEADERS, "Accept": "application/json"}, timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        postings = payload.get("content", [])
        for item in postings:
            posting_id = str(item.get("id", "")).strip()
            title = str(item.get("name", "")).strip()
            if not title or not posting_id:
                continue
            url = _smartrecruiters_public_url(company_id, item)
            consider(
                title, url,
                {"company": config["name"], "source": config["source"],
                 "location": _smartrecruiters_location(item.get("location")),
                 "external_id": posting_id,
                 "_health_source": "Portails ATS directs"},
                jobs, seen_urls,
            )
        offset += len(postings)
        if not postings or offset >= payload.get("totalFound", 0):
            break


def _scrape_successfactors_source(config: dict, jobs: list, seen_urls: set):
    """Lit les listings SuccessFactors modernes rendus côté serveur."""
    list_urls = config.get("list_urls") or [config["list_url"]]
    for list_url in list_urls:
        soup = fetch(list_url)
        if not soup:
            continue
        for anchor in soup.select("a[href*='/job/']"):
            title = _job_anchor_title(anchor)
            full_url = urljoin(list_url, anchor.get("href", ""))
            if not title or not re.search(r"/job/", urlparse(full_url).path):
                continue
            card = _job_card(anchor)
            context = card.get_text(" ", strip=True) if card else title
            location = extract_location_hint(context)
            if not location:
                geo = structured_geography(context)
                if geo["status"] != "unknown":
                    location = geo["evidence"]
            consider(
                title, full_url,
                {"company": config["name"], "source": config["source"],
                 "location": location, "description": context[:1800],
                 "_health_source": "Portails ATS directs"},
                jobs, seen_urls,
            )


def scrape_configured_ats() -> list:
    """Interroge les portails carrière publics listés dans ats_sources.json."""
    jobs, seen_urls = [], set()
    mark_raw_source("Portails ATS directs")
    adapters = {
        "workday": _scrape_workday_source,
        "smartrecruiters": _scrape_smartrecruiters_source,
        "successfactors": _scrape_successfactors_source,
    }
    for config in _load_ats_sources():
        profiles = config.get("profiles", [])
        if profiles and ACTIVE_PROFILE not in profiles:
            continue
        source = config.get("source", f"ATS – {config.get('name', '?')}")
        config["source"] = source
        mark_raw_source(source)
        adapter = adapters.get(config.get("type"))
        if not adapter:
            log(f"ATS ignoré ({config.get('name', '?')}): type non pris en charge")
            continue
        started = time.monotonic()
        before = len(jobs)
        sub_status, sub_error = "ok", ""
        try:
            adapter(config, jobs, seen_urls)
        except (KeyError, requests.RequestException, ValueError) as exc:
            log(f"ATS {config.get('name', '?')}: {exc}")
            sub_status, sub_error = "error", f"{type(exc).__name__}: {exc}"
        raw_count = _raw_counts.get(source, 0)
        if sub_status == "ok" and len(jobs) == before:
            sub_status = "filtered" if raw_count > 0 else "empty"
        subsources = getattr(_SCRAPER_RUN_LOCAL, "subsources", None)
        if subsources is not None:
            subsources.append({
                "name": f"ats_{normalize(config.get('name', source))}",
                "source_field": source,
                "count": len(jobs) - before,
                "raw": raw_count,
                "status": sub_status,
                "error": sub_error,
                "duration_ms": round((time.monotonic() - started) * 1000),
            })
    log(f"Portails ATS directs: {len(jobs)} offre(s) trouvée(s)")
    return jobs


# ---------------------------------------------------------------------------
# Auto-diagnostic de santé des sources (point 4)
# ---------------------------------------------------------------------------

def load_health() -> dict:
    health = _load_json_file(HEALTH_FILE, {})
    return health if isinstance(health, dict) else {}


def save_health(health: dict):
    _atomic_write_json(HEALTH_FILE, health)


def update_health(source: str, count: int, health: dict,
                  raw: int = None, source_field: str = None,
                  status: str = "ok", duration_ms: int | None = None,
                  error: str = "") -> list:
    """Met à jour l'historique et renvoie des alertes si une source dégénère.

    `raw` = nb de candidats BRUTS extraits (avant filtrage) si connu : distingue un
    sélecteur cassé (0 brut) d'une simple absence d'offre pertinente (brut > 0).
    `source_field` = champ « source » de la source, mémorisé pour retrouver son
    compte brut lors d'un run où elle ne renvoie plus aucune offre.
    """
    alerts = []
    entry = health.get(source, {})
    # Migration tolérante des anciens health.json.
    entry.setdefault("runs", 0)
    entry.setdefault("total", 0)
    entry.setdefault("last", None)
    entry.setdefault("max", 0)
    avg_before = (entry["total"] / entry["runs"]) if entry["runs"] else 0
    entry["runs"] += 1
    entry["total"] += count
    entry["last"] = count
    entry["max"] = max(entry["max"], count)
    now = local_now().isoformat()
    entry["updated_at"] = now
    entry["last_run_at"] = now
    entry["last_status"] = status
    if duration_ms is not None:
        entry["duration_ms"] = max(0, int(duration_ms))
    if status == "error":
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        entry["last_error"] = str(error or "échec sans détail")[:500]
    elif status == "disabled":
        entry["consecutive_failures"] = 0
        entry["last_error"] = str(error or "source non configurée")[:500]
    else:
        entry["consecutive_failures"] = 0
        if error:
            entry["last_error"] = str(error)[:500]
        else:
            entry.pop("last_error", None)
    if source_field:
        entry["source_field"] = source_field
    if raw is not None:
        entry["raw_last"] = raw
        entry["raw_max"] = max(entry.get("raw_max", 0), raw)
        entry["consecutive_raw_empty"] = (
            entry.get("consecutive_raw_empty", 0) + 1 if raw == 0 else 0
        )
    healthy = (
        status == "ok"
        or status == "filtered"
        or (raw is not None and raw > 0 and status not in ("error", "disabled"))
    )
    if healthy:
        entry["last_healthy_at"] = now
        # Clé historique conservée pour les anciens rapports/outils.
        entry["last_success_at"] = now
    health[source] = entry
    if status == "error":
        if entry["consecutive_failures"] == 1:
            alerts.append(
                f"🚨 {source_field or source} : exécution en échec"
                + (f" — {entry['last_error']}" if entry.get("last_error") else ".")
            )
        return alerts
    if status == "disabled":
        return alerts
    if source in HEALTH_SILENT_SOURCES:
        return alerts          # suivi conservé, mais pas d'alerte (source en sommeil)
    label = source_field or source
    # Source à extraction suivie (raw connu) : on se fie au compte BRUT, qui
    # distingue une vraie panne d'une simple absence d'offre pertinente. Détectable
    # dès le 1er run KO (pas besoin d'attendre N runs).
    if raw is not None:
        if raw == 0 and entry.get("raw_max", 0) > 0:
            alerts.append(
                f"🚨 {label} : 0 candidat brut extrait (jusqu'à {entry['raw_max']} "
                f"auparavant) — page/sélecteur probablement cassé."
            )
        elif raw == 0 and entry["runs"] >= 5:
            alerts.append(
                f"🔇 {label} : 0 candidat brut depuis {entry['runs']} runs "
                f"(jamais aucun résultat) — source à déboguer ou repointer."
            )
        # raw > 0 : la source fonctionne ; 0 offre PERTINENTE n'est pas une panne.
        return alerts
    # Sources sans signal brut (court-circuitent consider) : heuristique sur le count.
    # Détection de panne : la source produisait régulièrement, et tombe à 0
    if count == 0 and entry["max"] >= 1 and avg_before >= 0.5 and entry["runs"] > 2:
        alerts.append(
            f"🚨 {label} : 0 offre alors que la moyenne historique était "
            f"{avg_before:.1f} (max {entry['max']}). Sélecteur probablement cassé."
        )
    # Source chroniquement muette : n'a JAMAIS rien produit malgré plusieurs runs
    elif entry["max"] == 0 and entry["runs"] >= 5:
        alerts.append(
            f"🔇 {label} : 0 offre depuis {entry['runs']} runs (jamais aucun "
            f"résultat) — à déboguer ou repointer."
        )
    return alerts


def update_health_stage_metrics(health: dict, jobs: list[dict]):
    """Ajoute les étapes profil/revue/unique aux canaris d'extraction."""
    fields_to_entries = defaultdict(list)
    for entry in health.values():
        entry["main_last"] = 0
        entry["review_last"] = 0
        entry["unique_last"] = 0
        source_field = str(entry.get("source_field", ""))
        if source_field:
            fields_to_entries[source_field].append(entry)

    classified = []
    for job in jobs:
        decision = classify_job(job)
        destination = decision["destination"]
        if destination not in ("main", "review"):
            continue
        classified.append(job)
        for entry in fields_to_entries.get(str(job.get("source", "")), []):
            entry[f"{destination}_last"] += 1

    for job in deduplicate_jobs(classified):
        for entry in fields_to_entries.get(str(job.get("source", "")), []):
            entry["unique_last"] += 1


# ---------------------------------------------------------------------------
# Alertes email
# ---------------------------------------------------------------------------

def send_alert(new_jobs: list, health_alerts: list):
    """Envoie un récapitulatif par email : nouvelles offres + alertes de panne."""
    if (not new_jobs and not health_alerts) or not SMTP_PASS or not SMTP_FROM or not SMTP_TO:
        return
    parts = []
    if new_jobs:
        parts.append(f"{len(new_jobs)} nouvelle(s) offre(s) :\n")
        for j in sorted(new_jobs, key=lambda x: x.get("score", 0), reverse=True):
            taux = f" [{j['taux']}]" if j.get("taux") else ""
            parts.append(f"- ({j.get('score',0)} pts){taux} {j['title']} "
                         f"({j.get('source','?')})\n  {j['url']}")
    if health_alerts:
        parts.append("\n--- Alertes techniques ---")
        parts.extend(health_alerts)
    msg = MIMEText("\n".join(parts))
    subject = f"[find_job:{ACTIVE_PROFILE}] {len(new_jobs)} offre(s)"
    if health_alerts:
        subject += f" + {len(health_alerts)} alerte(s)"
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
        log(f"Email envoyé → {SMTP_TO}")
    except Exception as e:
        log(f"Erreur email : {e}")


# ---------------------------------------------------------------------------
# Rapport HTML interactif (valeurs dynamiques échappées, sans serveur)
# ---------------------------------------------------------------------------

def _nav_html(active: str = "", prefix: str = "") -> str:
    links = [
        ("accueil", "Accueil", f"{prefix}index.html"),
        *[(name, cfg["label"], f"{prefix}{name}/") for name, cfg in PROFILES.items()],
        ("status", "Couverture", f"{prefix}status.html"),
    ]
    items = []
    for key, label, href in links:
        current = ' aria-current="page"' if key == active else ""
        items.append(f'<a href="{escape(href)}"{current}>{escape(label)}</a>')
    return '<nav class="main-nav" aria-label="Navigation principale">' + "".join(items) + "</nav>"


def _age_label(found_at: str) -> str:
    try:
        days = max(0, (local_now() - parse_local_datetime(found_at)).days)
    except (TypeError, ValueError):
        return "Date inconnue"
    if days == 0:
        return "Aujourd’hui"
    if days == 1:
        return "Hier"
    return f"Il y a {days} j"


def _score_label(score: int) -> str:
    if score < MIN_SCORE:
        return "À vérifier"
    if score >= 6:
        return "Très pertinent"
    if score >= 4:
        return "Forte correspondance"
    return "Pertinent"


def generate_html(new_jobs: list, all_jobs: list, review_jobs: list | None = None):
    now = local_now().strftime("%d/%m/%Y à %H:%M")
    all_jobs = deduplicate_jobs(all_jobs)
    strict_ids = {job_id(job.get("title", ""), job.get("url", "")) for job in all_jobs}
    review_jobs = [
        job for job in deduplicate_jobs(review_jobs or [])
        if job_id(job.get("title", ""), job.get("url", "")) not in strict_ids
    ]
    new_ids = {job_id(j.get("title", ""), j.get("url", "")) for j in new_jobs}
    new_jobs = [
        job for job in all_jobs
        if job_id(job.get("title", ""), job.get("url", "")) in new_ids
    ]
    other_jobs = [
        job for job in all_jobs
        if job_id(job.get("title", ""), job.get("url", "")) not in new_ids
    ]

    def rows(job_list, is_new=False, is_review=False):
        output = []
        for job in job_list:
            legacy_jid = legacy_job_id(
                job.get("title", ""), job.get("url", "")
            )
            jid = tracking_id(job)
            title = clean_job_title(job.get("title", ""))
            company = job_employer(job) or "—"
            location = display_location(job.get("location", ""))
            source = job.get("source", "—")
            score = int(job.get("score", 0) or 0)
            found_at = job.get("found_at", "")
            try:
                found_date = parse_local_datetime(found_at).strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                found_date = "—"
            keywords = matched_keywords(job)
            details = []
            if job.get("taux"):
                details.append(job["taux"])
            for value in (job_contract(job), job_work_mode(job)):
                if value and value not in details:
                    details.append(value)
            if job.get("salary"):
                details.append(f"Salaire {job['salary']}")
            posted_date = job_posted_date(job)
            if posted_date:
                details.append(f"Publiée {posted_date}")
            deadline = job_deadline(job)
            if deadline:
                details.append(f"Échéance {deadline}")
            detail_html = (
                "".join(f'<span class="detail-chip">{escape(value)}</span>' for value in details)
                or '<span class="muted">Non précisé</span>'
            )
            keyword_html = (
                '<span class="keywords">Mots-clés : '
                + escape(", ".join(keywords)) + "</span>"
                if keywords else ""
            )
            if is_review and job.get("review_reason"):
                keyword_html += (
                    '<span class="review-reason">À vérifier : '
                    + escape(job["review_reason"]) + "</span>"
                )
            search_text = " ".join((title, company, location, source, " ".join(keywords)))
            output.append(
                f'<tr class="job-row{" new" if is_new else ""}{" review" if is_review else ""}" '
                f'data-id="{jid}" data-legacy-id="{legacy_jid}" '
                f'data-search="{escape(normalize(search_text))}" '
                f'data-source="{escape(source)}" data-location="{escape(location)}" '
                f'data-company="{escape(company)}" data-score="{score}" '
                f'data-date="{escape(found_at)}" data-title="{escape(normalize(title))}">'
                f'<td data-label="Poste"><a class="job-title" href="{escape(job.get("url", ""))}" '
                f'target="_blank" rel="noopener noreferrer">{escape(title)} '
                f'<span aria-hidden="true">↗</span></a>{keyword_html}</td>'
                f'<td data-label="Entreprise">{escape(company)}</td>'
                f'<td data-label="Lieu">{escape(location)}</td>'
                f'<td data-label="Conditions"><div class="details">{detail_html}</div></td>'
                f'<td data-label="Source">{escape(source)}</td>'
                f'<td data-label="Pertinence"><span class="score score-{min(score // 2, 3)}" '
                f'title="Score {score}. {escape(_score_label(score))}. '
                f'{escape("Mots-clés : " + ", ".join(keywords)) if keywords else ""}">'
                f'{escape(_score_label(score))} <strong>{score}</strong></span></td>'
                f'<td data-label="Ajoutée"><time datetime="{escape(found_at)}" '
                f'title="{escape(found_date)}">{escape(_age_label(found_at))}</time></td>'
                '<td data-label="Suivi"><div class="tracking">'
                '<button class="favorite" type="button" aria-label="Ajouter aux favoris" '
                'title="Ajouter aux favoris">☆</button>'
                '<select class="job-status" aria-label="État de la candidature">'
                '<option value="">À examiner</option><option value="applied">Candidature envoyée</option>'
                '<option value="ignored">Ignorée</option></select>'
                '<button class="hide-job secondary" type="button" title="Masquer cette offre">Masquer</button>'
                '</div></td></tr>\n'
            )
        return "".join(output)

    header = """<thead><tr>
<th scope="col"><button class="sort-button" data-sort="title">Poste</button></th>
<th scope="col"><button class="sort-button" data-sort="company">Entreprise</button></th>
<th scope="col">Lieu</th><th scope="col">Conditions</th><th scope="col">Source</th>
<th scope="col"><button class="sort-button" data-sort="score">Pertinence</button></th>
<th scope="col"><button class="sort-button" data-sort="date">Ajoutée</button></th>
<th scope="col">Suivi</th></tr></thead>"""

    def section(section_id, title, jobs, is_new=False, is_review=False):
        body = rows(jobs, is_new, is_review)
        table_hidden = "" if jobs else " hidden"
        empty_hidden = " hidden" if jobs else ""
        return f"""<section class="jobs-section" id="{section_id}">
<div class="section-heading"><h2>{escape(title)} <span class="count" data-count>{len(jobs)}</span></h2></div>
<div class="table-wrap"{table_hidden}><table><caption class="sr-only">{escape(title)}</caption>
{header}<tbody>{body}</tbody></table></div>
<p class="section-empty"{empty_hidden}>Aucune offre dans cette section.</p>
</section>"""

    displayed_jobs = all_jobs + review_jobs
    sources = sorted({str(j.get("source", "—")) for j in displayed_jobs})
    locations = sorted({display_location(j.get("location", "")) for j in displayed_jobs})
    source_options = "".join(f'<option value="{escape(v)}">{escape(v)}</option>' for v in sources)
    location_options = "".join(f'<option value="{escape(v)}">{escape(v)}</option>' for v in locations)

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(ACTIVE_PROFILE_CONFIG['description'])}">
<meta name="theme-color" content="#1d4ed8"><meta property="og:title" content="{escape(ACTIVE_PROFILE_CONFIG['title'])}">
<meta property="og:description" content="{escape(ACTIVE_PROFILE_CONFIG['description'])}">
<link rel="canonical" href="{escape(profile_url(ACTIVE_PROFILE))}">
<link rel="alternate" type="application/rss+xml" title="Flux RSS" href="feed.xml">
<link rel="manifest" href="../manifest.webmanifest"><link rel="icon" href="../icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="../assets/site.css">
<script>if(localStorage.getItem('find-job:theme')==='dark')document.documentElement.dataset.theme='dark';</script>
<title>{escape(ACTIVE_PROFILE_CONFIG['title'])}</title>
</head>
<body data-profile="{escape(ACTIVE_PROFILE)}">
<a class="skip-link" href="#offres">Aller aux offres</a>
<header class="site-header"><div class="shell header-inner"><a class="brand" href="../">Veille emploi</a>
{_nav_html(ACTIVE_PROFILE, '../')}<div class="header-actions"><a class="icon-button" href="feed.xml" title="Flux RSS">RSS</a>
<button id="theme-toggle" class="icon-button" type="button" aria-label="Changer de thème">◐</button></div></div></header>
<main class="shell" id="offres">
<section class="hero"><div><p class="eyebrow">Genève et district de Nyon proche</p>
<h1>{escape(ACTIVE_PROFILE_CONFIG['title'])}</h1>
<p class="updated">Mise à jour le {now}. Offres triées de la plus récente à la plus ancienne.</p></div>
<div class="hero-stats"><div><strong>{len(all_jobs)}</strong><span>offres</span></div>
<div><strong>{len(new_jobs)}</strong><span>nouvelles</span></div>
<div><strong>{len(review_jobs)}</strong><span>à vérifier</span></div></div></section>

<section class="panel filters" aria-labelledby="filters-title"><div class="panel-title-row">
<div><h2 id="filters-title">Rechercher et filtrer</h2><p>Les résultats se mettent à jour immédiatement.</p></div>
<button id="reset-filters" class="secondary" type="button">Réinitialiser</button></div>
<div class="filter-grid">
<label class="search-field">Recherche<input id="search" type="search" placeholder="Poste, entreprise, mot-clé…" autocomplete="off"></label>
<label>Lieu<select id="location-filter"><option value="">Tous les lieux</option>{location_options}</select></label>
<label>Source<select id="source-filter"><option value="">Toutes les sources</option>{source_options}</select></label>
<label>Score minimal<select id="score-filter"><option value="0">Tous</option><option value="2">2 — Pertinent</option>
<option value="4">4 — Forte correspondance</option><option value="6">6 — Très pertinent</option></select></label>
<label>Date d’ajout<select id="date-filter"><option value="0">Toutes</option><option value="1">24 heures</option>
<option value="7">7 jours</option><option value="30">30 jours</option></select></label>
<label>État<select id="status-filter"><option value="">Tous</option><option value="review">À examiner</option>
<option value="applied">Candidature envoyée</option><option value="ignored">Ignorée</option></select></label>
<label>Trier par<select id="sort-select"><option value="date-desc">Plus récentes</option><option value="score-desc">Meilleur score</option>
<option value="title-asc">Poste A–Z</option><option value="company-asc">Entreprise A–Z</option></select></label>
</div>
<div class="filter-toggles"><label><input id="favorites-only" type="checkbox"> Favoris uniquement</label>
<label><input id="show-hidden" type="checkbox"> Afficher les offres masquées</label></div>
</section>

<div class="results-bar"><p id="result-count" role="status" aria-live="polite">{len(displayed_jobs)} offres affichées</p>
<div><button id="export-tracking" class="secondary" type="button">Exporter le suivi</button>
<label class="button secondary" for="import-tracking">Importer le suivi</label>
<input id="import-tracking" class="sr-only" type="file" accept="application/json"></div></div>

<aside class="score-help"><strong>Comment lire la pertinence ?</strong> Un mot-clé dans le titre vaut 2 points et dans la description 1 point. Survolez un score pour voir les correspondances.</aside>
{section('nouvelles', 'Nouvelles offres', sorted(new_jobs, key=lambda j: (j.get('score', 0), j.get('found_at', '')), reverse=True), True)}
{section('autres', 'Autres offres', sorted(other_jobs, key=lambda j: (j.get('found_at', ''), j.get('score', 0)), reverse=True))}
{section('a-verifier', 'Offres à vérifier', sorted(review_jobs, key=lambda j: (j.get('score', 0), j.get('found_at', '')), reverse=True), False, True)}
</main>
<footer><div class="shell">Données issues de plusieurs plateformes d’emploi. Vérifiez toujours l’annonce d’origine avant de postuler.</div></footer>
<script src="../assets/report.js" defer></script>
</body></html>"""

    _atomic_write_text(RESULTS_FILE, html)
    _atomic_write_text(PUBLIC_FILE, html)
    log(f"Rapport HTML mis à jour : {RESULTS_FILE}")


def generate_rss(all_jobs: list):
    """Génère un flux RSS des offres (bonus, lisible en agrégateur/mobile)."""
    recent = sorted(
        deduplicate_jobs(all_jobs), key=lambda x: x["found_at"], reverse=True
    )[:50]
    items = ""
    for j in recent:
        title = escape(clean_job_title(j["title"]))
        link = escape(j["url"])
        src = escape(j.get("source", ""))
        loc = escape(display_location(j.get("location", "")))
        try:
            pub_dt = parse_local_datetime(j["found_at"])
        except (KeyError, ValueError):
            pub_dt = local_now()
        pub_date = pub_dt.astimezone(LOCAL_TIMEZONE).strftime(
            "%a, %d %b %Y %H:%M:%S %z"
        )
        items += (
            f"<item><title>{title}</title><link>{link}</link>"
            f"<pubDate>{pub_date}</pubDate>"
            f"<description>{loc} — {src}</description>"
            f"<guid isPermaLink='false'>{escape(job_id(j['title'], j['url']))}</guid>"
            f"</item>\n"
        )
    rss = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<rss version='2.0'><channel>"
        f"<title>{escape(ACTIVE_PROFILE_CONFIG['rss_title'])}</title>"
        f"<link>{escape(profile_url(ACTIVE_PROFILE))}</link>"
        f"<description>{escape(ACTIVE_PROFILE_CONFIG['description'])}</description>\n"
        f"{items}"
        "</channel></rss>"
    )
    _atomic_write_text(RSS_FILE, rss)


SITE_CSS = r"""
:root{color-scheme:light;--bg:#f6f8fc;--surface:#fff;--surface-2:#eef3fb;--text:#172033;--muted:#5c667a;--border:#dbe2ee;--primary:#1d4ed8;--primary-dark:#173ea6;--warning:#9a5a00;--shadow:0 12px 35px rgba(23,32,51,.08);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
:root[data-theme="dark"]{color-scheme:dark;--bg:#101522;--surface:#171e2d;--surface-2:#222b3d;--text:#edf2ff;--muted:#aeb9cd;--border:#334058;--primary:#84a8ff;--primary-dark:#b2c7ff;--warning:#f7c46c;--shadow:0 12px 35px rgba(0,0,0,.3)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);line-height:1.5}a{color:var(--primary);text-decoration-thickness:.08em;text-underline-offset:.16em}button,input,select{font:inherit}button,.button{cursor:pointer}.shell{width:min(1440px,calc(100% - 2rem));margin-inline:auto}.skip-link{position:fixed;left:1rem;top:-5rem;z-index:100;background:var(--surface);padding:.7rem 1rem;border-radius:.5rem}.skip-link:focus{top:1rem}.site-header{position:sticky;top:0;z-index:30;background:color-mix(in srgb,var(--surface) 92%,transparent);border-bottom:1px solid var(--border);backdrop-filter:blur(12px)}.header-inner{min-height:64px;display:flex;align-items:center;gap:1.25rem}.brand{font-weight:850;color:var(--text);text-decoration:none;white-space:nowrap}.main-nav{display:flex;align-items:center;gap:.25rem;overflow:auto}.main-nav a{padding:.55rem .7rem;color:var(--muted);text-decoration:none;border-radius:.5rem;white-space:nowrap;font-size:.92rem}.main-nav a:hover,.main-nav a[aria-current="page"]{background:var(--surface-2);color:var(--text)}.header-actions{display:flex;gap:.4rem;margin-left:auto}.icon-button,.secondary,.button{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:.55rem;padding:.48rem .72rem;text-decoration:none;font-weight:650}.icon-button:hover,.secondary:hover,.button:hover{border-color:var(--primary);color:var(--primary)}main{padding-block:2.25rem 4rem}.hero{display:flex;justify-content:space-between;gap:2rem;align-items:flex-end;margin-bottom:1.5rem}.eyebrow{color:var(--primary);font-weight:750;text-transform:uppercase;letter-spacing:.08em;font-size:.78rem;margin:0 0 .35rem}.hero h1{font-size:clamp(1.85rem,4vw,3rem);line-height:1.08;margin:0;max-width:900px}.updated{color:var(--muted);margin:.7rem 0 0}.hero-stats{display:flex;gap:.65rem}.hero-stats div{min-width:95px;background:var(--surface);border:1px solid var(--border);border-radius:.8rem;padding:.7rem 1rem;text-align:center;box-shadow:var(--shadow)}.hero-stats strong{display:block;font-size:1.55rem}.hero-stats span{color:var(--muted);font-size:.82rem}.panel{background:var(--surface);border:1px solid var(--border);border-radius:1rem;padding:1.15rem;box-shadow:var(--shadow)}.panel-title-row{display:flex;align-items:start;justify-content:space-between;gap:1rem}.panel h2{font-size:1.15rem;margin:0}.panel p{color:var(--muted);margin:.2rem 0 1rem}.filter-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem}.search-field{grid-column:span 2}.filter-grid label{min-width:0;font-size:.78rem;color:var(--muted);font-weight:700}.filter-grid input,.filter-grid select{display:block;width:100%;margin-top:.25rem;min-height:42px;border:1px solid var(--border);border-radius:.55rem;background:var(--bg);color:var(--text);padding:.55rem .65rem}.filter-grid input:focus,.filter-grid select:focus,button:focus-visible,a:focus-visible{outline:3px solid color-mix(in srgb,var(--primary) 35%,transparent);outline-offset:2px}.filter-toggles{display:flex;gap:1.25rem;flex-wrap:wrap;margin-top:.9rem;font-size:.9rem}.results-bar{display:flex;justify-content:space-between;align-items:center;gap:1rem;margin:1.2rem 0}.results-bar p{font-weight:750;margin:0}.results-bar>div{display:flex;gap:.5rem;flex-wrap:wrap}.score-help{background:color-mix(in srgb,var(--primary) 8%,var(--surface));border-left:4px solid var(--primary);padding:.75rem 1rem;border-radius:.35rem;color:var(--muted);font-size:.9rem}.score-help strong{color:var(--text)}.jobs-section{margin-top:2rem}.section-heading{border-bottom:2px solid var(--border)}.section-heading h2{font-size:1.35rem;margin:0;padding-bottom:.5rem}.count{display:inline-grid;place-items:center;min-width:1.7rem;height:1.7rem;padding:0 .4rem;border-radius:99px;background:var(--primary);color:var(--bg);font-size:.78rem;vertical-align:middle}.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:.8rem;background:var(--surface);margin-top:1rem;box-shadow:var(--shadow)}table{border-collapse:separate;border-spacing:0;width:100%;font-size:.9rem}th{background:var(--surface-2);color:var(--text);padding:.65rem .7rem;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}td{padding:.72rem;border-bottom:1px solid var(--border);vertical-align:top}tbody tr:last-child td{border-bottom:0}tbody tr:hover td{background:color-mix(in srgb,var(--primary) 5%,var(--surface))}.job-row.new td{background:color-mix(in srgb,#facc15 10%,var(--surface))}.job-row.is-favorite td:first-child{box-shadow:inset 4px 0 var(--warning)}.job-row.is-hidden{opacity:.58}.job-title{font-weight:760}.keywords{display:block;color:var(--muted);font-size:.76rem;margin-top:.3rem;max-width:38rem}.details{display:flex;gap:.3rem;flex-wrap:wrap}.detail-chip,.score{display:inline-block;border-radius:99px;padding:.18rem .48rem;font-size:.76rem;white-space:nowrap}.detail-chip{background:var(--surface-2)}.score{background:color-mix(in srgb,var(--primary) 12%,var(--surface));color:var(--primary-dark)}.score strong{margin-left:.2rem}.muted,time{color:var(--muted);white-space:nowrap}.tracking{display:flex;align-items:center;gap:.35rem;min-width:255px}.tracking select{max-width:155px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:.45rem;padding:.36rem}.tracking button{padding:.36rem .48rem}.favorite{border:0;background:transparent;color:var(--warning);font-size:1.45rem;line-height:1}.sort-button{border:0;background:transparent;color:inherit;font-weight:750;padding:0}.sort-button::after{content:" ↕";color:var(--muted)}.sort-button[data-direction="asc"]::after{content:" ↑"}.sort-button[data-direction="desc"]::after{content:" ↓"}.section-empty{color:var(--muted);background:var(--surface);border:1px dashed var(--border);border-radius:.7rem;padding:1rem}.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}[hidden]{display:none!important}footer{border-top:1px solid var(--border);padding:1.5rem 0;color:var(--muted);font-size:.85rem;background:var(--surface)}
.job-row.review td{background:color-mix(in srgb,#f59e0b 7%,var(--surface))}.review-reason{display:block;color:var(--warning);font-size:.76rem;margin-top:.3rem;font-weight:650}.status-profile{margin-bottom:1.5rem}.status-profile h3{margin:1.4rem 0 .4rem}.freshness-warning{color:var(--warning)!important;background:color-mix(in srgb,#f59e0b 12%,var(--surface));border-left:4px solid var(--warning);padding:.65rem .8rem;border-radius:.35rem;font-weight:750}
.portal-hero{padding:2rem 0 1rem}.portal-hero h1{font-size:clamp(2.1rem,6vw,4rem);line-height:1.02;margin:.3rem 0;max-width:850px}.portal-intro{font-size:1.1rem;color:var(--muted);max-width:760px}.coverage{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:1rem}.coverage span{background:var(--surface-2);border-radius:99px;padding:.35rem .65rem;font-size:.83rem}.profile-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-top:1.5rem}.profile-card{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:1rem;padding:1.15rem;box-shadow:var(--shadow)}.profile-card h2{margin:.25rem 0;font-size:1.3rem}.profile-card>p{color:var(--muted)}.card-stats{display:flex;gap:.6rem;margin:.5rem 0 1rem}.card-stats span{background:var(--surface-2);border-radius:.5rem;padding:.4rem .55rem;font-size:.82rem}.latest{border-top:1px solid var(--border);padding-top:.8rem;margin-top:auto}.latest h3{font-size:.82rem;text-transform:uppercase;color:var(--muted);letter-spacing:.05em}.latest ul{padding-left:1.1rem}.latest li{margin:.35rem 0;font-size:.88rem}.card-actions{display:flex;gap:.5rem;align-items:center;margin-top:1rem}.primary-button{display:inline-block;background:var(--primary);color:var(--bg);border-radius:.55rem;padding:.55rem .75rem;text-decoration:none;font-weight:750}.primary-button:hover{background:var(--primary-dark)}.rss-link{font-size:.85rem}.last-update{font-size:.78rem!important}
@media(max-width:1150px){.profile-grid{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.shell{width:min(100% - 1rem,1440px)}.header-inner{align-items:flex-start;flex-wrap:wrap;padding:.65rem 0}.main-nav{order:3;width:100%}.site-header{position:relative}.hero{align-items:flex-start;flex-direction:column}.hero-stats{width:100%}.hero-stats div{flex:1}.filter-grid{grid-template-columns:1fr 1fr}.search-field{grid-column:1/-1}.results-bar{align-items:flex-start;flex-direction:column}.profile-grid{grid-template-columns:1fr}.table-wrap{overflow:visible;border:0;background:transparent;box-shadow:none}table,thead,tbody,tr,td{display:block;width:100%}thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}tbody{display:grid;gap:.75rem}.job-row{border:1px solid var(--border);border-radius:.8rem;background:var(--surface);box-shadow:var(--shadow);overflow:hidden}.job-row td{display:grid;grid-template-columns:95px 1fr;gap:.5rem;border-bottom:1px solid var(--border);padding:.65rem}.job-row td::before{content:attr(data-label);color:var(--muted);font-size:.75rem;font-weight:750}.job-row td:first-child{display:block}.job-row td:first-child::before{display:none}.job-row td:last-child{border-bottom:0}.tracking{min-width:0;flex-wrap:wrap}.tracking select{max-width:100%}th{position:static}}
@media(max-width:480px){.filter-grid{grid-template-columns:1fr}.search-field{grid-column:auto}.panel-title-row{align-items:flex-start;flex-direction:column}.job-row td{grid-template-columns:80px 1fr}.header-actions{position:absolute;right:0;top:.55rem}}
@media print{.site-header,.filters,.results-bar,.tracking,footer,.score-help{display:none!important}body{background:#fff;color:#000}.shell{width:100%}.table-wrap{box-shadow:none}.jobs-section{break-inside:avoid}.job-row.is-hidden{display:none!important}}
"""


REPORT_JS = r"""
(()=>{'use strict';
const body=document.body,profile=body.dataset.profile||'global',storageKey=`find-job:v2:${profile}`,byId=id=>document.getElementById(id),rows=()=>[...document.querySelectorAll('.job-row')];
const normalize=value=>(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase().trim();let state={jobs:{}};try{state=JSON.parse(localStorage.getItem(storageKey))||state}catch(_e){}if(!state.jobs)state.jobs={};
const save=()=>localStorage.setItem(storageKey,JSON.stringify(state)),record=id=>state.jobs[id]||(state.jobs[id]={favorite:false,status:'',hidden:false});
let migrated=false;const rowRecord=row=>{const id=row.dataset.id,legacy=row.dataset.legacyId;if(!state.jobs[id]&&legacy&&state.jobs[legacy]){state.jobs[id]=state.jobs[legacy];migrated=true}return record(id)};
function paintRow(row){const item=rowRecord(row),favorite=row.querySelector('.favorite'),status=row.querySelector('.job-status'),hide=row.querySelector('.hide-job');row.classList.toggle('is-favorite',!!item.favorite);row.classList.toggle('is-hidden',!!item.hidden);favorite.textContent=item.favorite?'★':'☆';favorite.setAttribute('aria-label',item.favorite?'Retirer des favoris':'Ajouter aux favoris');favorite.title=favorite.getAttribute('aria-label');hide.textContent=item.hidden?'Réafficher':'Masquer';hide.title=item.hidden?'Réafficher cette offre':'Masquer cette offre';status.value=item.status||'';row.dataset.status=item.status||'review'}rows().forEach(paintRow);if(migrated)save();
document.addEventListener('click',event=>{const favorite=event.target.closest('.favorite'),hide=event.target.closest('.hide-job');if(favorite){const row=favorite.closest('.job-row'),item=rowRecord(row);item.favorite=!item.favorite;paintRow(row);save();applyFilters()}if(hide){const row=hide.closest('.job-row'),item=rowRecord(row);item.hidden=!item.hidden;paintRow(row);save();applyFilters()}});
document.addEventListener('change',event=>{if(event.target.matches('.job-status')){const row=event.target.closest('.job-row');rowRecord(row).status=event.target.value;paintRow(row);save();applyFilters()}});
const controls=['search','location-filter','source-filter','score-filter','date-filter','status-filter','sort-select','favorites-only','show-hidden'];controls.forEach(id=>{const node=byId(id);node?.addEventListener(node.type==='search'?'input':'change',applyFilters)});
function sortRows(value){const [key,direction]=value.split('-'),factor=direction==='asc'?1:-1;document.querySelectorAll('tbody').forEach(tbody=>{[...tbody.children].sort((a,b)=>{let av=a.dataset[key]||'',bv=b.dataset[key]||'';if(key==='score')return(Number(av)-Number(bv))*factor;return av.localeCompare(bv,'fr',{numeric:true,sensitivity:'base'})*factor}).forEach(row=>tbody.appendChild(row))});document.querySelectorAll('.sort-button').forEach(button=>{button.removeAttribute('data-direction');button.closest('th').removeAttribute('aria-sort')});const active=document.querySelector(`.sort-button[data-sort="${key}"]`);if(active){active.dataset.direction=direction;active.closest('th').setAttribute('aria-sort',direction==='asc'?'ascending':'descending')}}
function applyFilters(){const query=normalize(byId('search').value),location=byId('location-filter').value,source=byId('source-filter').value,minScore=Number(byId('score-filter').value),days=Number(byId('date-filter').value),status=byId('status-filter').value,favorites=byId('favorites-only').checked,showHidden=byId('show-hidden').checked,cutoff=days?Date.now()-days*86400000:0;let visible=0;rows().forEach(row=>{const item=rowRecord(row),matches=(!query||row.dataset.search.includes(query))&&(!location||row.dataset.location===location)&&(!source||row.dataset.source===source)&&Number(row.dataset.score)>=minScore&&(!cutoff||new Date(row.dataset.date).getTime()>=cutoff)&&(!status||(item.status||'review')===status)&&(!favorites||item.favorite)&&(showHidden||!item.hidden);row.hidden=!matches;if(matches)visible++});document.querySelectorAll('.jobs-section').forEach(section=>{const count=[...section.querySelectorAll('.job-row')].filter(row=>!row.hidden).length;section.querySelector('[data-count]').textContent=count;section.querySelector('.table-wrap').hidden=count===0;section.querySelector('.section-empty').hidden=count!==0});byId('result-count').textContent=`${visible} offre${visible>1?'s':''} affichée${visible>1?'s':''}`;sortRows(byId('sort-select').value)}
document.querySelectorAll('.sort-button').forEach(button=>button.addEventListener('click',()=>{const key=button.dataset.sort,current=button.dataset.direction||'',direction=current==='desc'?'asc':'desc';byId('sort-select').value=`${key}-${direction}`;applyFilters()}));
byId('reset-filters')?.addEventListener('click',()=>{controls.forEach(id=>{const node=byId(id);if(!node)return;if(node.type==='checkbox')node.checked=false;else node.value=id==='sort-select'?'date-desc':''});byId('score-filter').value='0';byId('date-filter').value='0';applyFilters()});
byId('export-tracking')?.addEventListener('click',()=>{const payload={version:2,profile,exportedAt:new Date().toISOString(),jobs:state.jobs},blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`suivi-candidatures-${profile}.json`;link.click();URL.revokeObjectURL(url)});
byId('import-tracking')?.addEventListener('change',event=>{const file=event.target.files[0];if(!file)return;const reader=new FileReader();reader.onload=()=>{try{const payload=JSON.parse(reader.result);if(!payload.jobs||typeof payload.jobs!=='object')throw new Error();state.jobs={...state.jobs,...payload.jobs};save();rows().forEach(paintRow);applyFilters();byId('result-count').textContent='Suivi importé.'}catch(_e){alert('Ce fichier de suivi est invalide.')}};reader.readAsText(file);event.target.value=''});
byId('theme-toggle')?.addEventListener('click',()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'';localStorage.setItem('find-job:theme',dark?'dark':'light')});if('serviceWorker'in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('../sw.js').catch(()=>{}));applyFilters();})();
"""


def generate_site_assets():
    """Écrit les ressources partagées et le mode hors-ligne de GitHub Pages."""
    assets = DOCS_ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(assets / "site.css", SITE_CSS.strip() + "\n")
    _atomic_write_text(assets / "report.js", REPORT_JS.strip() + "\n")
    manifest = {"name": "Veille emploi – Genève", "short_name": "Veille emploi", "start_url": "./", "display": "standalone", "background_color": "#f6f8fc", "theme_color": "#1d4ed8", "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}
    _atomic_write_text(
        DOCS_ROOT / "manifest.webmanifest",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    icon = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="104" fill="#1d4ed8"/><path fill="white" d="M128 157h256v220H128z"/><path fill="#1d4ed8" d="M201 123h110a35 35 0 0 1 35 35v28h-31v-25a9 9 0 0 0-9-9H206a9 9 0 0 0-9 9v25h-31v-28a35 35 0 0 1 35-35zm-73 123h256v42H128z"/></svg>"""
    _atomic_write_text(DOCS_ROOT / "icon.svg", icon)
    cached = ["./", "./index.html", "./status.html", "./publication.json", "./assets/site.css", "./assets/report.js", "./icon.svg", "./manifest.webmanifest"] + [f"./{profile}/" for profile in PROFILES]
    sw = "const CACHE='find-job-v2';const FILES=" + json.dumps(cached) + ";self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES))));self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));self.addEventListener('fetch',e=>{if(e.request.method!=='GET'||new URL(e.request.url).origin!==location.origin)return;e.respondWith(fetch(e.request).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));return r}).catch(()=>caches.match(e.request)))})\n"
    _atomic_write_text(DOCS_ROOT / "sw.js", sw)


def generate_status_page(published_at: datetime | None = None):
    """Publie les signaux permettant d'identifier une perte de couverture."""
    published_at = published_at or local_now()
    published_iso = published_at.isoformat()
    published_label = published_at.strftime("%d/%m/%Y à %H:%M")
    status_labels = {
        "ok": "OK",
        "filtered": "Filtrée",
        "empty": "Vide",
        "warning": "Avertissement",
        "disabled": "Désactivée",
        "error": "Erreur",
    }
    sections = []
    for profile, cfg in PROFILES.items():
        data_dir = DATA_ROOT / profile
        health = _load_json_file(data_dir / "health.json", {})
        coverage = _load_json_file(data_dir / "query_coverage.json", {})
        rejections = _load_json_file(data_dir / "rejections.json", {})
        health = health if isinstance(health, dict) else {}
        coverage = coverage if isinstance(coverage, dict) else {}
        rejections = rejections if isinstance(rejections, dict) else {}
        health_rows_data = []
        latest_profile_run = max(
            (str(entry.get("last_run_at") or entry.get("updated_at") or "")
             for entry in health.values()),
            default="",
        )
        for name, entry in sorted(health.items()):
            status = entry.get("last_status", "—")
            duration = entry.get("duration_ms")
            duration_label = f"{duration / 1000:.1f} s" if isinstance(duration, int) else "—"
            last_run = str(
                entry.get("last_run_at") or entry.get("updated_at") or ""
            ).replace("T", " ")[:16] or "—"
            last_healthy = str(
                entry.get("last_healthy_at") or entry.get("last_success_at") or ""
            ).replace("T", " ")[:16] or "—"
            error = str(entry.get("last_error", ""))[:160] or "—"
            health_rows_data.append(
                f"<tr><td>{escape(entry.get('source_field', name))}</td>"
                f"<td>{escape(status_labels.get(status, status))}</td>"
                f"<td>{entry.get('last', 0)}</td><td>{entry.get('raw_last', '—')}</td>"
                f"<td>{entry.get('unique_last', '—')}</td>"
                f"<td>{entry.get('main_last', '—')}</td>"
                f"<td>{entry.get('review_last', '—')}</td>"
                f"<td>{escape(duration_label)}</td><td>{escape(last_run)}</td>"
                f"<td>{escape(last_healthy)}</td>"
                f"<td>{escape(error)}</td></tr>"
            )
        health_rows = "".join(health_rows_data) or (
            '<tr><td colspan="11">Aucune donnée disponible.</td></tr>'
        )
        silent_queries = [
            (*key.split("::", 1), entry)
            for key, entry in coverage.items() if entry.get("last") == 0
        ]
        query_rows = "".join(
            f"<tr><td>{escape(source)}</td><td>{escape(query)}</td>"
            f"<td>{entry.get('zero_runs', 0)}</td>"
            f"<td>{entry.get('max', 0)}</td></tr>"
            for source, query, entry in sorted(
                silent_queries, key=lambda item: (item[0], item[1])
            )[:30]
        ) or '<tr><td colspan="4">Aucune régression détectée.</td></tr>'
        rejection_rows = "".join(
            f"<tr><td>{escape(reason.replace('_', ' '))}</td><td>{count}</td></tr>"
            for reason, count in sorted(rejections.get("counts", {}).items())
        ) or '<tr><td colspan="2">Le prochain passage alimentera ce journal.</td></tr>'
        by_source_rows_data = []
        for source, counts in rejections.get("by_source", {}).items():
            for reason, count in counts.items():
                by_source_rows_data.append((count, source, reason))
        by_source_rows = "".join(
            f"<tr><td>{escape(source)}</td><td>{escape(reason.replace('_', ' '))}</td>"
            f"<td>{count}</td></tr>"
            for count, source, reason in sorted(
                by_source_rows_data, key=lambda item: (-item[0], item[1], item[2])
            )[:30]
        ) or '<tr><td colspan="3">Le prochain passage alimentera ce journal.</td></tr>'
        freshness = (
            f'<p class="freshness-warning" data-last-run="{escape(latest_profile_run)}" '
            f'hidden>⚠️ Le dernier passage complet de ce profil est ancien.</p>'
        )
        sections.append(f"""<section class="panel status-profile"><h2>{escape(cfg['label'])}</h2>{freshness}
<h3>Sources</h3><div class="table-wrap"><table><thead><tr><th>Source</th><th>État</th><th>Candidats profil</th><th>Candidats bruts</th><th>Uniques du passage</th><th>Sélection</th><th>À vérifier</th><th>Durée</th><th>Dernier passage</th><th>Dernier passage sain</th><th>Diagnostic</th></tr></thead><tbody>{health_rows}</tbody></table></div>
<h3>Requêtes actuellement muettes</h3><div class="table-wrap"><table><thead><tr><th>Source</th><th>Requête</th><th>Passages à zéro</th><th>Maximum historique</th></tr></thead><tbody>{query_rows}</tbody></table></div>
<h3>Motifs de rejet du dernier passage</h3><div class="table-wrap"><table><thead><tr><th>Motif</th><th>Nombre</th></tr></thead><tbody>{rejection_rows}</tbody></table></div>
<h3>Rejets par source</h3><div class="table-wrap"><table><thead><tr><th>Source</th><th>Motif</th><th>Nombre</th></tr></thead><tbody>{by_source_rows}</tbody></table></div></section>""")
    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="État de couverture des sources de la veille emploi."><link rel="stylesheet" href="assets/site.css"><link rel="icon" href="icon.svg" type="image/svg+xml"><title>Couverture de la recherche</title></head>
<body data-last-published="{escape(published_iso)}"><header class="site-header"><div class="shell header-inner"><a class="brand" href="./">Veille emploi</a>{_nav_html('status')}<div class="header-actions"><button id="theme-toggle" class="icon-button" type="button">◐</button></div></div></header>
<main class="shell"><section class="hero"><div><p class="eyebrow">Diagnostic</p><h1>Couverture de la recherche</h1><p class="updated">Publication générée le {escape(published_label)}. Cette page permet de repérer une source cassée, une requête devenue muette ou un filtre trop strict.</p><p id="publication-warning" class="freshness-warning" hidden>⚠️ La dernière publication remonte à plus de 36 heures.</p></div></section>{''.join(sections)}</main>
<script>document.getElementById('theme-toggle').addEventListener('click',()=>{{const d=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=d?'dark':'';localStorage.setItem('find-job:theme',d?'dark':'light')}});if(localStorage.getItem('find-job:theme')==='dark')document.documentElement.dataset.theme='dark';const stale=value=>{{const date=Date.parse(value);return !date||Date.now()-date>36*3600*1000}};if(stale(document.body.dataset.lastPublished))document.getElementById('publication-warning').hidden=false;document.querySelectorAll('.status-profile .freshness-warning').forEach(node=>{{if(stale(node.dataset.lastRun))node.hidden=false}});</script></body></html>"""
    _atomic_write_text(DOCS_ROOT / "status.html", html)


def generate_portal_index():
    """Tableau de bord des profils avec compteurs et dernières offres."""
    now = local_now()
    generate_site_assets()
    generate_status_page(now)
    _atomic_write_json(
        DOCS_ROOT / "publication.json",
        {"last_published_at": now.isoformat()},
    )
    cards = []
    for profile, cfg in PROFILES.items():
        path = DATA_ROOT / profile / "all_jobs.json"
        jobs = _load_json_file(path, [])
        if not isinstance(jobs, list):
            jobs = []
        jobs = deduplicate_jobs(jobs)
        review_path = DATA_ROOT / profile / "review_jobs.json"
        review_data = _load_json_file(review_path, [])
        review_count = (
            len(deduplicate_jobs(review_data)) if isinstance(review_data, list) else 0
        )
        recent = sorted(jobs, key=lambda job: job.get("found_at", ""), reverse=True)
        added_24h = 0
        for job in jobs:
            try:
                if now - parse_local_datetime(job.get("found_at", "")) <= timedelta(hours=24):
                    added_24h += 1
            except (TypeError, ValueError):
                pass
        latest = "".join(
            f'<li><a href="{escape(job.get("url", ""))}" target="_blank" rel="noopener noreferrer">'
            f'{escape(clean_job_title(job.get("title", "")))}</a></li>'
            for job in recent[:3]
        ) or "<li>Aucune offre pour le moment.</li>"
        updated = (
            datetime.fromtimestamp(path.stat().st_mtime, LOCAL_TIMEZONE).strftime(
                "%d/%m/%Y à %H:%M"
            )
            if path.exists() else "jamais"
        )
        cards.append(f"""<article class="profile-card">
<p class="eyebrow">{escape(cfg['label'])}</p><h2><a href="{profile}/">{escape(cfg['title'])}</a></h2>
<p>{escape(cfg['description'])}</p><div class="card-stats"><span><strong>{len(jobs)}</strong> offres</span>
<span><strong>{added_24h}</strong> ajoutées sur 24 h</span>
<span><strong>{review_count}</strong> à vérifier</span></div>
<div class="latest"><h3>Dernières offres</h3><ul>{latest}</ul></div>
<p class="last-update">Dernière actualisation : {updated}</p>
<div class="card-actions"><a class="primary-button" href="{profile}/">Voir les offres</a>
<a class="rss-link" href="{profile}/feed.xml">Flux RSS</a></div></article>""")
    html = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#1d4ed8">
<meta name="description" content="Veilles d'offres d'emploi à Genève et dans le district de Nyon proche.">
<meta property="og:title" content="Veilles emploi – Genève"><link rel="canonical" href="{escape(SITE_BASE_URL)}">
<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="assets/site.css"><script>if(localStorage.getItem('find-job:theme')==='dark')document.documentElement.dataset.theme='dark';</script>
<title>Veilles emploi – Genève</title></head><body><a class="skip-link" href="#profils">Aller aux profils</a>
<header class="site-header"><div class="shell header-inner"><a class="brand" href="./">Veille emploi</a>{_nav_html('accueil')}
<div class="header-actions"><button id="theme-toggle" class="icon-button" type="button" aria-label="Changer de thème">◐</button></div></div></header>
<main class="shell"><section class="portal-hero"><p class="eyebrow">Recherche ciblée et actualisée</p>
<h1>Vos veilles emploi à Genève</h1><p class="portal-intro">Trois sélections spécialisées, filtrées pour ne conserver que Genève et les communes proches du district de Nyon.</p>
<div class="coverage" aria-label="Zone couverte"><span>Canton de Genève</span><span>Nyon et communes proches</span><span>Offres francophones</span></div></section>
<section id="profils" class="profile-grid" aria-label="Profils de recherche">{''.join(cards)}</section></main>
<footer><div class="shell">Les compteurs sont actualisés lors de chaque recherche automatique.</div></footer>
<script>document.getElementById('theme-toggle').addEventListener('click',()=>{{const d=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=d?'dark':'';localStorage.setItem('find-job:theme',d?'dark':'light')}});if('serviceWorker'in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('sw.js').catch(()=>{{}}));</script>
</body></html>"""
    _atomic_write_text(DOCS_ROOT / "index.html", html)


# ---------------------------------------------------------------------------
# Persistance des offres
# ---------------------------------------------------------------------------

def load_all_jobs() -> list:
    p = DATA_DIR / "all_jobs.json"
    jobs = _load_json_file(p, [])
    return jobs if isinstance(jobs, list) else []


def save_all_jobs(jobs: list):
    p = DATA_DIR / "all_jobs.json"
    _atomic_write_json(p, jobs)


def load_review_jobs() -> list:
    jobs = _load_json_file(REVIEW_FILE, [])
    return jobs if isinstance(jobs, list) else []


def save_review_jobs(jobs: list):
    _atomic_write_json(REVIEW_FILE, jobs)


@dataclass(frozen=True)
class SourceSpec:
    """Fonction et politique opérationnelle d'une source, définies au même endroit."""
    name: str
    scraper: Callable[[], list]
    source_field: str
    profiles: frozenset[str] = frozenset()
    requires_browser: bool = False
    health_silent: bool = False

    def enabled_for(self, profile: str) -> bool:
        return not self.profiles or profile in self.profiles


SYSTEMES_PROFILE = frozenset({"systemes"})

# Registre unique : ajouter une source ne nécessite plus de synchroniser quatre listes.
SOURCE_SPECS = (
    SourceSpec("ville_geneve", scrape_ville_geneve, "geneve.ch"),
    SourceSpec("ville_nyon", scrape_ville_nyon, "nyon.ch"),
    SourceSpec("ge_ch", scrape_ge_ch, "ge.ch"),
    SourceSpec("vaud", scrape_vaud, "offres-emploi.vd.ch"),
    SourceSpec("unige", scrape_unige, "jobs.unige.ch"),
    SourceSpec("myscience", scrape_myscience, "myscience.ch", requires_browser=True),
    SourceSpec("museums", scrape_museums, "museums.ch"),
    SourceSpec("educa", scrape_educa, "recrutement.hesge.ch"),
    SourceSpec("educh", scrape_educh, "educh.ch"),
    SourceSpec("ecolint", scrape_ecolint, "ecolint.ch"),
    SourceSpec("bibliosuisse", scrape_bibliosuisse, "bibliosuisse.ch", health_silent=True),
    SourceSpec("letemps", scrape_letemps, "Le Temps Emploi"),
    SourceSpec("jobscout24", scrape_jobscout24, "jobscout24.ch"),
    SourceSpec("jobup", scrape_jobup, "jobup.ch"),
    SourceSpec("indeed_pw", scrape_indeed_pw, "Indeed CH", requires_browser=True, health_silent=True),
    SourceSpec("adzuna", scrape_adzuna, "Adzuna (Indeed+)"),
    SourceSpec("jobs_ch_pw", scrape_jobs_ch_pw, "jobs.ch", requires_browser=True, health_silent=True),
    SourceSpec("linkedin_alert_emails", scrape_linkedin_alert_emails, "LinkedIn (alerte email)"),
    SourceSpec("configured_ats", scrape_configured_ats, "Portails ATS directs", health_silent=True),
    SourceSpec("swissdevjobs", scrape_swissdevjobs, "swissdevjobs.ch", SYSTEMES_PROFILE),
    SourceSpec("itjobs_ch", scrape_itjobs_ch, "itjobs.ch", SYSTEMES_PROFILE),
    SourceSpec("itboard", scrape_itboard, "itboard.ch", SYSTEMES_PROFILE),
    SourceSpec("cern", scrape_cern, "careers.cern"),
    SourceSpec("icrc", scrape_icrc, "careers.icrc.org"),
    SourceSpec("wipo", scrape_wipo, "wipo.taleo.net"),
    SourceSpec("who", scrape_who, "careers.who.int", requires_browser=True),
    SourceSpec("unicef", scrape_unicef, "jobs.unicef.org"),
    SourceSpec("pictet", scrape_pictet, "careers.pictet.com", requires_browser=True),
    SourceSpec("job_room", scrape_job_room, "job-room.ch", SYSTEMES_PROFILE, True),
    SourceSpec("sig", scrape_sig, "jobs.sig-ge.ch"),
    SourceSpec("tpg", scrape_tpg, "tpg.ch", requires_browser=True),
    SourceSpec("un_geneva", scrape_un_geneva, "careers.un.org", requires_browser=True),
    SourceSpec("wto", scrape_wto, "wto.org"),
    SourceSpec("reliefweb", scrape_reliefweb, "reliefweb.int"),
    SourceSpec("cagi", scrape_cagi, "jobs.cagi.ch", requires_browser=True),
    SourceSpec("cinfo", scrape_cinfo, "cinfo.ch"),
)

# Alias dérivés conservés pour compatibilité avec les tests et appels ciblés.
SCRAPERS = [spec.scraper for spec in SOURCE_SPECS]
SCRAPER_SOURCE_FIELDS = {spec.name: spec.source_field for spec in SOURCE_SPECS}
SYSTEMES_ONLY_SCRAPERS = tuple(
    spec.scraper for spec in SOURCE_SPECS if spec.profiles == SYSTEMES_PROFILE
)
PLAYWRIGHT_SCRAPERS = tuple(
    spec.scraper for spec in SOURCE_SPECS if spec.requires_browser
)
HEALTH_SILENT_SOURCES = {
    spec.name for spec in SOURCE_SPECS if spec.health_silent
}


def active_source_specs(profile: str) -> list[SourceSpec]:
    return [spec for spec in SOURCE_SPECS if spec.enabled_for(profile)]


def _run_source(spec: SourceSpec) -> list[dict]:
    """Exécute une source isolément et conserve son résultat opérationnel."""
    started = time.monotonic()
    _SCRAPER_RUN_LOCAL.diagnostics = []
    _SCRAPER_RUN_LOCAL.status_hint = ""
    _SCRAPER_RUN_LOCAL.subsources = []
    try:
        results = spec.scraper()
        if not isinstance(results, list):
            raise TypeError(
                f"{spec.scraper.__name__} doit renvoyer une liste, reçu "
                f"{type(results).__name__}"
            )
        diagnostics = list(_SCRAPER_RUN_LOCAL.diagnostics)
        status_hint = _SCRAPER_RUN_LOCAL.status_hint
        subsources = list(_SCRAPER_RUN_LOCAL.subsources)
        return [{
            "name": spec.name,
            "results": results,
            "status": status_hint or ("warning" if diagnostics else "ok"),
            "error": " | ".join(diagnostics[-3:]),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "subsources": subsources,
        }]
    except PlaywrightBrowserUnavailable as exc:
        message = _playwright_error_summary(exc)
        log(f"{spec.scraper.__name__} : {message} — source ignorée")
        return [{
            "name": spec.name, "results": [], "status": "disabled",
            "error": message,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }]
    except Exception as exc:
        error_text = (
            _playwright_error_summary(exc) if spec.requires_browser else str(exc)
        )
        log(f"⚠️  {spec.scraper.__name__} a échoué : {error_text}")
        return [{
            "name": spec.name, "results": [], "status": "error",
            "error": f"{type(exc).__name__}: {error_text}",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }]
    finally:
        del _SCRAPER_RUN_LOCAL.diagnostics
        del _SCRAPER_RUN_LOCAL.status_hint
        del _SCRAPER_RUN_LOCAL.subsources


def collect_source_outcomes(profile: str) -> list[dict]:
    """Parallélise HTTP et sérialise Playwright, indépendamment de la persistance."""
    global _DEFER_DETAIL_FETCHES
    specs = active_source_specs(profile)
    browser_specs = [spec for spec in specs if spec.requires_browser]
    http_specs = [spec for spec in specs if not spec.requires_browser]

    def run_browser_group():
        outcomes = []
        for spec in browser_specs:
            outcomes.extend(_run_source(spec))
        return outcomes

    collected = []
    _DEFER_DETAIL_FETCHES = True
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(_run_source, spec) for spec in http_specs]
            if browser_specs:
                futures.append(pool.submit(run_browser_group))
            for future in as_completed(futures):
                collected.extend(future.result())
    finally:
        _DEFER_DETAIL_FETCHES = False
    _process_pending_detail_candidates()
    return collected


def run_profile(profile: str):
    global _detail_fetch_count, _employer_fetch_count, _raw_counts
    global _query_counts, _rejection_counts, _rejection_samples, _rejection_by_source
    global _detail_source_yield
    configure_profile(profile)
    bootstrap_legacy_profile_data()
    with _COUNTERS_LOCK:
        _detail_fetch_count = 0
        _employer_fetch_count = 0
        _raw_counts = {}
        _query_counts = {}
        _rejection_counts = {}
        _rejection_samples = {}
        _rejection_by_source = {}
        _pending_detail_candidates.clear()
    _load_detail_cache()

    log(f"=== Démarrage de la recherche d'emploi ({ACTIVE_PROFILE_CONFIG['label']}) ===")
    seen = load_seen()
    all_jobs = load_all_jobs()
    review_jobs = load_review_jobs()
    health = load_health()
    _detail_source_yield = {}
    for entry in health.values():
        source_field = normalize(entry.get("source_field", ""))
        raw_previous = int(entry.get("raw_last", 0) or 0)
        unique_previous = int(entry.get("unique_last", 0) or 0)
        if source_field:
            # Lissage : une petite source spécialisée reste prioritaire, tandis
            # qu'un gros agrégateur peu productif ne monopolise pas la fin du quota.
            _detail_source_yield[source_field] = (
                (unique_previous + 1) / (raw_previous + 5)
            )
    all_jobs, seen = expire_old_jobs(all_jobs, seen)
    review_jobs, _ = expire_old_jobs(review_jobs, set())
    before = len(all_jobs)
    all_jobs = [
        j for j in all_jobs
        if is_french_text(j.get("title", ""), j.get("description", ""))
    ]
    removed = before - len(all_jobs)
    if removed:
        log(f"Nettoyage : {removed} offre(s) non-francophone(s) retirée(s) de l'archive")

    # Ré-validation de l'archive avec les filtres ACTUELS (pertinence, zone, score).
    # Purge les entrées captées sous d'anciennes règles (FLE, labo, hors-zone…).
    # On recalcule le score car les listes de mots-clés ont pu changer.
    before = len(all_jobs)
    revalidated = []
    migrated_review = []
    for j in all_jobs:
        finalize(j)
        decision = classify_job(j)
        if decision["destination"] == "main":
            j.pop("review_reason", None)
            j.pop("_review", None)
            revalidated.append(j)
        elif decision["destination"] == "review":
            j["review_reason"] = decision["reason"]
            migrated_review.append(j)
    all_jobs = revalidated
    review_jobs.extend(migrated_review)
    purged = before - len(all_jobs)
    if purged:
        log(f"Ré-validation : {purged} offre(s) archivée(s) désormais hors critères retirée(s)")

    # Les offres en revue sont réévaluées avec les nouveaux mots-clés : elles
    # peuvent être promues automatiquement dans la sélection principale.
    retained_review = []
    for job in review_jobs:
        finalize(job)
        decision = classify_job(job)
        if decision["destination"] == "main":
            job.pop("review_reason", None)
            job.pop("_review", None)
            job["_new"] = True
            all_jobs.append(job)
        elif decision["destination"] == "review":
            job["review_reason"] = decision["reason"]
            retained_review.append(job)
    review_jobs = retained_review

    # Purge des liens morts : une offre dont la page renvoie 404/410 a été retirée
    # par la source et ne doit plus figurer dans le rapport (lien cassé).
    # Parallélisé (requêtes HEAD indépendantes) pour ne pas allonger le run.
    before = len(all_jobs)
    jobs_to_check = [job for job in all_jobs if dead_link_check_due(job)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        dead_flags = list(pool.map(lambda j: url_is_dead(j["url"]), jobs_to_check))
    checked_at = local_now().isoformat()
    dead_urls = {
        canonical_url(job["url"])
        for job, is_dead in zip(jobs_to_check, dead_flags) if is_dead
    }
    for job, is_dead in zip(jobs_to_check, dead_flags):
        if not is_dead:
            job["url_checked_at"] = checked_at
    all_jobs = [
        job for job in all_jobs
        if canonical_url(job.get("url", "")) not in dead_urls
    ]
    dead = before - len(all_jobs)
    if dead:
        log(f"Liens morts : {dead} offre(s) retirée(s) (page 404/410)")

    # Réconciliation `seen` ↔ archive : une offre sortie de `all_jobs` (fusion,
    # expiration, ré-validation, lien mort) ne doit PAS rester « déjà vue », sinon
    # `if jid in seen: continue` l'empêcherait de réapparaître même redevenue
    # pertinente ou distincte (cas des doublons mal fusionnés réintroduits depuis).
    seen = {job_id(j["title"], j["url"]) for j in all_jobs}

    raw = []
    health_alerts = []
    collected = collect_source_outcomes(profile)

    # Agrégation SÉQUENTIELLE dans le thread principal (pas de race sur raw/health).
    for outcome in collected:
        source_name = outcome["name"]
        results = outcome["results"]
        raw.extend(results)
        # Canari : compte de candidats bruts (clé = champ « source », appris des
        # résultats ou mémorisé d'un run précédent). raw=0 n'est retenu que pour une
        # source qui extrayait avant ; sinon None = pas de signal brut fiable (p. ex.
        # sources qui court-circuitent consider()).
        sf = (
            SCRAPER_SOURCE_FIELDS.get(source_name)
            or (results[0].get("source") if results else "")
            or health.get(source_name, {}).get("source_field")
        )
        had_raw = health.get(source_name, {}).get("raw_max", 0) > 0
        raw_n = (_raw_counts.get(sf, 0)
                 if (sf and (sf in _raw_counts or had_raw)) else None)
        status = outcome["status"]
        if status not in ("error", "warning", "disabled"):
            if results:
                status = "ok"
            elif raw_n is not None and raw_n > 0:
                status = "filtered"
            else:
                status = "empty"
        health_alerts.extend(
            update_health(
                source_name,
                len(results),
                health,
                raw=raw_n,
                source_field=sf,
                status=status,
                duration_ms=outcome["duration_ms"],
                error=outcome["error"],
            )
        )
        for subsource in outcome.get("subsources", []):
            health_alerts.extend(update_health(
                subsource["name"], subsource["count"], health,
                raw=subsource["raw"],
                source_field=subsource["source_field"],
                status=subsource["status"],
                duration_ms=subsource["duration_ms"],
                error=subsource["error"],
            ))

    update_health_stage_metrics(health, raw)
    health_alerts.extend(update_query_coverage())

    # Nouvelles offres : enrichissement employeur + même gate de pertinence.
    new_jobs = []
    new_review_jobs = []
    for job in raw:
        jid = job_id(job["title"], job["url"])
        if jid in seen:
            continue                # déjà vu à l'identique : inutile d'enrichir
        # Enrichissement employeur : si la source n'a pas donné d'employeur, on
        # lit la page de détail pour en extraire l'école/organisation. Fait ici
        # (et pas avant) pour ne fetcher que les offres réellement nouvelles.
        if not is_meaningful_company(job.get("company", "")):
            desc = job.get("description", "") or fetch_employer_page(job["url"])
            job["description"] = desc
            emp = extract_employer(f"{job.get('title', '')} {desc}")
            if emp:
                job["employer"] = emp
        finalize(job)
        decision = classify_job(job)
        if decision["destination"] == "main":
            job.pop("review_reason", None)
            job.pop("_review", None)
            seen.add(jid)
            job["_new"] = True
            new_jobs.append(job)
            continue
        if decision["destination"] == "review":
            job.pop("_review", None)
            job["review_reason"] = decision["reason"]
            job["_new_review"] = True
            new_review_jobs.append(job)
        else:
            record_rejection(decision["reason"], job)

    # Fusion des doublons sur (archive + nouvelles offres). On traite les offres
    # à employeur CONNU d'abord (canoniques, absorbent les variantes sans
    # employeur), puis par ordre d'ancienneté. Deux écoles distinctes au même
    # intitulé restent séparées (employeurs connus différents).
    combined = all_jobs + new_jobs
    deduped = deduplicate_jobs(combined)
    merged = len(combined) - len(deduped)
    if merged:
        log(f"Fusion doublons : {merged} offre(s) en double regroupée(s)")
    all_jobs = deduped
    new_jobs = [j for j in deduped if j.pop("_new", False)]
    strict_ids = {job_id(j["title"], j["url"]) for j in all_jobs}
    review_jobs = [
        job for job in deduplicate_jobs(review_jobs + new_review_jobs)
        if job_id(job["title"], job["url"]) not in strict_ids
    ]
    new_review_jobs = [job for job in review_jobs if job.pop("_new_review", False)]
    # Recalage final : `seen.json` doit refléter l'archive réellement publiée,
    # après purge et fusion des doublons.
    seen = {job_id(j["title"], j["url"]) for j in all_jobs}

    log(f"Nouvelles offres : {len(new_jobs)} | Total cumulé : {len(all_jobs)} "
        f"| À vérifier : {len(review_jobs)} ({len(new_review_jobs)} nouvelle(s)) "
        f"| Pages de détail lues : {_detail_fetch_count} "
        f"| Pages employeur lues : {_employer_fetch_count}")
    for alert in health_alerts:
        log(alert)

    save_seen(seen)
    save_all_jobs(all_jobs)
    save_review_jobs(review_jobs)
    save_health(health)
    save_rejection_report()
    _save_detail_cache()
    generate_html(new_jobs, all_jobs, review_jobs)
    generate_rss(all_jobs)
    send_alert(new_jobs, health_alerts)
    log(f"=== Recherche terminée ({ACTIVE_PROFILE_CONFIG['label']}) ===\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scraper d'offres d'emploi par profil de recherche."
    )
    choices = sorted(PROFILES) + ["all"]
    profile_help = f"Profil à lancer : {', '.join(choices)}."
    parser.add_argument(
        "profile",
        nargs="?",
        choices=choices,
        help=profile_help,
    )
    parser.add_argument(
        "-p", "--profile",
        dest="profile_opt",
        choices=choices,
        help=profile_help,
    )
    args = parser.parse_args(argv)
    env_profile = os.environ.get("JOB_PROFILE")
    profile = args.profile_opt or args.profile or env_profile or DEFAULT_RUN_PROFILE
    if profile not in choices:
        parser.error(f"profil inconnu: {profile}")
    return profile


def _main_unlocked(profile: str):
    if profile == "all":
        for name in PROFILES:
            run_profile(name)
        generate_portal_index()
        return
    run_profile(profile)
    generate_portal_index()


def main(argv=None):
    profile = parse_args(argv)
    try:
        with scraper_process_lock(), shared_run_cache():
            return _main_unlocked(profile)
    except ScraperAlreadyRunning as exc:
        print(str(exc))
        return 0


if __name__ == "__main__":
    main()
