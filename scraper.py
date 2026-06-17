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
import hashlib
import smtplib
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse, urljoin

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
            "       Pour activer Indeed : sudo apt install chromium-browser"
        )
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    _CHROMIUM_PATH = None

# ---------------------------------------------------------------------------
# Configuration (secrets chargés depuis l'environnement / .env)
# ---------------------------------------------------------------------------

ADZUNA_ID = os.environ.get("ADZUNA_ID", "")
ADZUNA_KEY = os.environ.get("ADZUNA_KEY", "")

SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO = os.environ.get("SMTP_TO", "")

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
# Indeed est bloqué par un mur anti-bot persistant (renvoie 0) et coûte ~50 s via
# Playwright : désactivé par défaut. Réactivable avec ENABLE_INDEED=1 sans le retirer.
ENABLE_INDEED = os.environ.get("ENABLE_INDEED", "0") not in ("0", "false", "False")
# Budget dédié à l'extraction de l'employeur (lecture des pages de détail des
# offres nouvelles sans entreprise). Séparé pour ne pas concurrencer ci-dessus.
MAX_EMPLOYER_FETCHES = int(os.environ.get("MAX_EMPLOYER_FETCHES", "40"))

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR = BASE_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_jobs.json"
RESULTS_FILE = DATA_DIR / "results.html"
PUBLIC_FILE = DOCS_DIR / "index.html"
LOG_FILE = DATA_DIR / "scraper.log"
HEALTH_FILE = DATA_DIR / "health.json"      # historique de santé des sources
RSS_FILE = DOCS_DIR / "feed.xml"            # flux RSS en sortie (bonus)

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
}

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    # Verrou : évite l'entrelacement des lignes quand les scrapers tournent en parallèle.
    with _LOG_LOCK:
        print(line)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def normalize(text: str) -> str:
    """Minuscule + suppression des accents pour un matching robuste."""
    text = text.lower()
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
_TEACHING_RE = _compile_terms(TEACHING_TERMS)
_SUBJECTS_RE = _compile_terms(LETTRES_SUBJECTS)

# Marqueurs de titre générique : déclenchent la lecture de la description.
_AMBIGUOUS_MARKERS = [
    "collaborateur", "collaboratrice", "assistant", "assistante",
    "charge de mission", "chargee de mission", "charge de projet",
    "chargee de projet", "specialiste", "responsable", "adjoint",
    "coordinateur", "coordinatrice", "gestionnaire", "conseiller",
    "conseillere", "agent", "stagiaire",
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

# Détection de langue — mots exclusivement allemands (normalize() enlève les accents)
_DE_STRONG = {
    "pflegefachfrau", "pflegefachmann", "pflegefachperson",
    "nachtwache", "ausbildung", "verantwortung", "bewerber",
    "stellenanzeige", "fachverantwortung", "privatstation", "arbeitszeit",
    "dienstleistung", "anforderungen",
}
_DE_COMMON = {
    "und", "fur", "nach", "beim", "stelle", "kenntnisse", "haben", "sein",
}


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
    text = normalize(title + " " + description)
    if term_in(text, _EXCLUDE_RE):
        return False
    if term_in(text, _KW_RE):
        return True
    if term_in(text, _TEACHING_RE) and term_in(text, _SUBJECTS_RE):
        return True
    return False


def is_french_text(title: str) -> bool:
    """Retourne False si le titre est clairement en allemand.

    Politique tolérante : en cas de doute on garde l'offre plutôt que de la rater.
    """
    words = set(normalize(title).split())
    if words & _DE_STRONG:
        return False
    if len(words & _DE_COMMON) >= 2:
        return False
    return True


def title_is_ambiguous(title: str) -> bool:
    """Vrai si le titre ne matche pas seul mais mérite qu'on lise la description.

    Cas typique : titres génériques (« collaborateur », « assistant »,
    « chargé de mission ») qui peuvent cacher un poste Lettres.
    """
    t = normalize(title)
    if term_in(t, _EXCLUDE_RE):
        return False                       # exclu d'office, inutile d'aller plus loin
    if term_in(t, _KW_RE):
        return False                       # déjà pertinent, pas besoin du détail
    return term_in(t, _AMBIGUOUS_RE)


def expire_old_jobs(all_jobs: list, seen: set) -> tuple:
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


# --- Cache des parseurs robots.txt par domaine ---
_ROBOTS_CACHE: dict = {}


def robots_allows(url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    from urllib.robotparser import RobotFileParser
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    rp = _ROBOTS_CACHE.get(base, "MISS")
    if rp == "MISS":
        # On récupère le robots.txt avec NOTRE User-Agent (SESSION) : la lib
        # urllib.read() utilise l'UA « Python-urllib » que certains sites (educh.ch)
        # bloquent en 403, ce qui faisait conclure à tort « tout interdit ». On lit
        # donc exactement le robots.txt qui s'applique à nos requêtes réelles.
        rp = RobotFileParser()
        try:
            r = SESSION.get(urljoin(base, "/robots.txt"), timeout=10)
            if r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None          # pas de robots.txt exploitable → pas de restriction
        except Exception:
            rp = None
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


def host_resolves(url: str) -> bool:
    """Vrai si le nom d'hôte de l'URL résout en DNS.

    Permet de sauter proprement un domaine hors-ligne (ex. job.educa.ch, NXDOMAIN)
    SANS tenter un fetch voué à l'échec qui polluerait les logs d'« Erreur fetch ».
    """
    host = urlparse(url).hostname
    if not host:
        return False
    if host not in _DNS_CACHE:
        try:
            socket.getaddrinfo(host, None)
            _DNS_CACHE[host] = True
        except OSError:
            _DNS_CACHE[host] = False
    return _DNS_CACHE[host]


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


def fetch(url: str, retries: int = 3):
    """GET poli avec respect de robots.txt, délai par domaine et back-off.

    Les erreurs permanentes (DNS mort, 403, 404) coupent court : pas de retry.
    Les vraies erreurs transitoires (timeout, 5xx) gardent les tentatives + back-off.
    """
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
    if not host_resolves(url):
        return False
    try:
        _polite_wait(url)
        r = SESSION.head(url, timeout=10, allow_redirects=True)
        if r.status_code == 405:           # HEAD refusé : on retente en GET léger
            _polite_wait(url)
            r = SESSION.get(url, timeout=15)
        return r.status_code in (404, 410)
    except Exception:
        return False


# --- Compteurs globaux de fetches de détail (garde-fous séparés) ---
_detail_fetch_count = 0      # lecture des titres ambigus (consider)
_employer_fetch_count = 0    # extraction de l'employeur (déduplication)


def _page_text(url: str) -> str:
    """Récupère le texte visible de la page de détail d'une offre (sans budget)."""
    soup = fetch(url, retries=2)
    if not soup:
        return ""
    # On retire les éléments non pertinents puis on extrait le texte visible
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(" ", strip=True)
    return text[:3000]      # borne pour rester raisonnable


def fetch_description(url: str) -> str:
    """Texte de détail pour lever l'ambiguïté d'un titre (point 1).

    Respecte MAX_DETAIL_FETCHES pour ne pas exploser le nombre de requêtes.
    Retourne "" si désactivé, quota atteint, ou échec.
    """
    global _detail_fetch_count
    if not FETCH_DESCRIPTIONS or _detail_fetch_count >= MAX_DETAIL_FETCHES:
        return ""
    _detail_fetch_count += 1
    return _page_text(url)


def fetch_employer_page(url: str) -> str:
    """Texte de détail pour extraire l'employeur d'une offre sans entreprise.

    Budget dédié (MAX_EMPLOYER_FETCHES), indépendant des fetches de titres
    ambigus, pour ne pas gonfler le volume de requêtes du scraping lui-même.
    """
    global _employer_fetch_count
    if _employer_fetch_count >= MAX_EMPLOYER_FETCHES:
        return ""
    _employer_fetch_count += 1
    return _page_text(url)


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
    if expect_results and not jobs:
        log(f"⚠️  {source}: 0 offre — sélecteur potentiellement cassé ou source bloquée")


def dedup_by_url(jobs: list) -> list:
    seen_urls, unique = set(), []
    for j in jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique.append(j)
    return unique


def finalize(job: dict) -> dict:
    """Complète une offre : score de pertinence + taux d'activité.

    À appeler juste avant d'ajouter l'offre à la liste retournée.
    """
    desc = job.get("description", "")
    job.setdefault("description", "")
    job["score"] = relevance_score(job["title"], desc)
    if not job.get("taux"):
        job["taux"] = extract_taux(job["title"] + " " + desc)
    return job


def passes_filters(job: dict) -> bool:
    """Gate de pertinence unique : exclusions, mots-clés, zone géo et score min.

    Appliqué à TOUTES les offres (scrapers + ré-validation de l'archive), pour
    une décision uniforme quel que soit le scraper d'origine.
    """
    title = job.get("title", "")
    desc = job.get("description", "")
    if not is_relevant(title, desc):
        return False
    # Lieu hors-zone mentionné dans le TITRE (ex. « … Musée Jenisch Vevey ») :
    # in_zone ne regarde que lieu+description, on couvre donc aussi le titre.
    if term_in(normalize(title), _GEO_FAR_RE):
        return False
    if not in_zone(job.get("location", ""), desc):
        return False
    score = job.get("score")
    if score is None:
        score = relevance_score(title, desc)
    return score >= MIN_SCORE


# ---------------------------------------------------------------------------
# Identité de l'employeur (sert à la déduplication basée sur le contenu)
# ---------------------------------------------------------------------------

# Valeurs « bouche-trou » posées par les scrapers qui ignorent l'employeur.
EMPLOYER_PLACEHOLDERS = {"", "—", "-", "n/a"}

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
    return job.get("employer", "")


def title_fingerprint(title: str) -> str:
    """Empreinte d'un titre, insensible à la typographie inclusive et à la ponctuation.

    « Greffier-ère », « Greffier·ère », « Greffier/ère » → même empreinte.
    Sert à fusionner les ré-publications d'une même offre.
    """
    return re.sub(r"[^a-z0-9]+", "", normalize(title))


def is_duplicate(job: dict, fp_to_known: dict) -> bool:
    """Vrai si `job` est un doublon d'une offre déjà retenue.

    Règle (choix utilisateur) : deux offres au même `title_fingerprint` sont des
    doublons, SAUF si toutes deux ont un employeur CONNU et DIFFÉRENT (cas « deux
    écoles distinctes au même intitulé », préservé via l'extraction d'employeur).

    `fp_to_known` mappe empreinte → set des employeurs connus déjà retenus.
    Met à jour `fp_to_known` au passage (enregistre l'offre conservée).
    """
    fp = title_fingerprint(job.get("title", ""))
    emp = normalize(job_employer(job))
    known = fp_to_known.get(fp)
    if known is None:
        fp_to_known[fp] = {emp} if emp else set()
        return False                       # empreinte jamais vue → on garde
    if emp and emp not in known:
        known.add(emp)
        return False                       # employeur connu et distinct → on garde
    return True                            # même titre, pas de nouvel employeur → doublon


# Zone géographique acceptée : Genève + district de Nyon proche
GEO_OK = GENEVE_ZONE | VAUD_ZONE

# Lieux explicitement trop loin → rejet immédiat (même si autres indices)
GEO_FAR = [
    "lausanne", "morges", "gland", "rolle", "yverdon", "vevey", "montreux",
    "fribourg", "neuchatel", "neuchâtel", "sion", "valais", "berne", "bern",
    "zurich", "zürich", "bale", "bâle", "basel", "lucerne", "luzern",
    "biel", "bienne", "delemont", "delémont", "jura", "aigle", "bulle",
    "pully", "renens", "vverdon", "winterthur", "saint-gall", "tessin",
    "lugano", "thoune", "coire", "chur", "schaffhouse", "zoug", "zug",
]

# Matching « mot entier » des lieux : « sion » (Valais) ne doit pas matcher
# « expreSSION », ni « bern » matcher « BERNex » (commune genevoise).
_GEO_FAR_RE = _compile_terms(GEO_FAR, inflect=False)
_GEO_OK_RE = _compile_terms(GEO_OK, inflect=False)


def in_zone(location: str, description: str = "") -> bool:
    """Vrai si l'offre est dans la zone Genève + Nyon proche.

    Politique : tolérante mais sûre.
    - Un lieu connu hors-zone (Lausanne, Fribourg…) → rejet.
    - Un lieu de la zone (Genève, Nyon…) → accepté.
    - Aucun indice de lieu → accepté par prudence (mieux vaut vérifier une
      offre de trop qu'en rater une mal étiquetée).
    """
    text = normalize(location + " " + description)
    if term_in(text, _GEO_FAR_RE):
        return False
    if term_in(text, _GEO_OK_RE):
        return True
    return True            # pas d'indice clair → on garde (à vérifier à l'œil)


def consider(title: str, url: str, base_fields: dict, jobs: list, seen_urls: set):
    """Logique commune : pertinence (titre puis description si ambigu),
    filtre géographique, enrichissement et ajout.

    base_fields doit contenir au moins company, source, location.
    """
    if not title or not url or url in seen_urls:
        return
    if not is_french_text(title):
        log(f"Rejeté (langue non-FR) : {title[:70]}")
        return
    description = ""
    if is_relevant(title):
        # Pertinent sur le titre seul. MAIS un poste « enseignant/formateur de
        # français » peut être du FLE (hors profil) : si le titre touche au
        # français/langues, on lit la fiche et on rejette UNIQUEMENT si un terme
        # FLE y figure (test ciblé : pas sur le bruit de page / autres annonces).
        # La fiche n'est lue QUE pour ce test : on ne la stocke pas comme
        # description (elle peut contenir d'autres annonces/menus qui fausseraient
        # la ré-validation par passes_filters()).
        if fle_risk(title) and is_fle(title, fetch_description(url)):
            log(f"Rejeté (FLE détecté dans la fiche) : {title[:70]}")
            return
    elif title_is_ambiguous(title):
        description = fetch_description(url)     # on lit le détail
        if not is_relevant(title, description):
            return
    else:
        return
    # Filtre géographique : on écarte les offres hors zone Genève/Nyon
    if not in_zone(base_fields.get("location", ""), description):
        return
    seen_urls.add(url)
    job = {
        "title": title, "url": url,
        "description": description,
        "found_at": datetime.now().isoformat(),
        **base_fields,
    }
    jobs.append(finalize(job))


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


def scrape_letemps() -> list:
    """Le Temps Emploi — page de listing."""
    jobs, seen_urls = [], set()
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
        consider(title, href,
                 {"company": company, "source": "Le Temps Emploi",
                  "location": location}, jobs, seen_urls)
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
            if not is_french_text(title):
                log(f"Rejeté (langue non-FR) : {title[:70]}")
                continue
            if is_relevant(title, short_desc):
                j = {
                    "title": title, "company": "État de Vaud",
                    "url": f"https://offres-emploi.vd.ch/#fr/job/{jid}",
                    "source": "offres-emploi.vd.ch", "location": loc,
                    "description": short_desc,
                    "found_at": datetime.now().isoformat(),
                }
                jobs.append(finalize(j))
                found += 1
        log(f"offres-emploi.vd.ch: {found} offre(s) trouvée(s) sur {len(reqs)} total")
    except Exception as e:
        log(f"Erreur scrape_vaud: {e}")
    return jobs


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
                # --- SÉLECTEUR ROBUSTE : liens d'offres par motif d'URL ---
                links = soup.select("a.job-link-detail, a.job-title, a[href*='/fr/job/']")
                for link in links:
                    href = link.get("href", "")
                    if "/fr/job/" not in href:
                        continue
                    title = (link.get("title", "") or link.get_text(strip=True)).strip()
                    if not title:
                        continue
                    full_url = BASE + href if href.startswith("/") else href
                    if full_url in seen_urls:
                        continue
                    # Lieu/entreprise : on cherche dans le conteneur parent proche
                    container = link.find_parent(["li", "article", "div"])
                    location = "—"
                    if container:
                        spans = container.select("p.job-attributes span, .job-location, .location")
                        if spans:
                            texts = [s.get_text(strip=True) for s in spans]
                            # heuristique : le lieu est souvent le 2e attribut
                            location = texts[1] if len(texts) > 1 else texts[0]
                    # Le filtre géographique fin est de toute façon dans consider()
                    consider(title, full_url,
                             {"company": "—", "source": "jobscout24.ch",
                              "location": location}, jobs, seen_urls)
            except Exception as e:
                log(f"Erreur jobscout24 [{kw}/{region_code}]: {e}")

    _warn_if_empty("jobscout24.ch", jobs)
    log(f"jobscout24.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


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
    SEARCH_CONFIGS = [("region=34", None), ("location=nyon", VAUD_ZONE)]
    jobs, seen_urls = [], set()

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
                    consider(title, full_url,
                             {"company": "—", "source": "jobup.ch",
                              "location": loc}, jobs, seen_urls)
            except Exception as e:
                log(f"Erreur jobup [{kw}/{geo_param}]: {e}")

    _warn_if_empty("jobup.ch", jobs)
    log(f"jobup.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


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
                desc = item.get("description", "")[:600]
                company = item.get("company", {}).get("display_name", "—")
                location = item.get("location", {}).get("display_name", "—")
                dedup_key = urlparse(link).path
                if not title or not link or dedup_key in seen_urls:
                    continue
                if not is_french_text(title):
                    log(f"Rejeté (langue non-FR) : {title[:70]}")
                    continue
                if not in_zone(location, desc):
                    continue
                # Adzuna fournit déjà une description : on l'exploite directement
                if not is_relevant(title, desc):
                    continue
                seen_urls.add(dedup_key)
                j = {
                    "title": title, "company": company, "url": link,
                    "source": "Adzuna (Indeed+)", "location": location,
                    "description": desc,
                    "found_at": datetime.now().isoformat(),
                }
                jobs.append(finalize(j))
        except Exception as e:
            log(f"Adzuna [{kw}]: {e}")
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


def fetch_via_playwright(url: str, wait_selector: str = None):
    """Charge une page derrière un mur JS via un Chromium furtif.

    Laisse le vrai navigateur exécuter le défi anti-bot (ex. POST /bot_score de
    myScience) puis renvoie le HTML rendu sous forme de BeautifulSoup.
    Retourne None si Chromium est indisponible ou en cas d'échec.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with _sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=_CHROMIUM_PATH, headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            ctx = _new_stealth_context(browser)
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=25000)
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
        return BeautifulSoup(html, "lxml")
    except Exception as e:
        log(f"Erreur fetch_via_playwright {url}: {e}")
        return None


INDEED_QUERIES = [
    ("rédacteur", "Genève"), ("éditeur", "Genève"), ("correcteur", "Genève"),
    ("bibliothécaire", "Genève"), ("traducteur", "Genève"),
    ("médiateur culturel", "Genève"), ("archiviste", "Genève"),
    ("journaliste", "Genève"), ("chargé de projet culturel", "Genève"),
]


def scrape_indeed_pw() -> list:
    """Offres Indeed CH via Playwright. Nécessite un Chromium système (apt)."""
    if not ENABLE_INDEED:
        return []                # désactivé par défaut (anti-bot) — cf. ENABLE_INDEED
    if not PLAYWRIGHT_AVAILABLE:
        log("Indeed : Chromium système introuvable — source ignorée (sudo apt install chromium-browser)")
        return []
    jobs, seen_urls = [], set()
    with _sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_CHROMIUM_PATH, headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
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
                    location = loc_el.get_text(strip=True) if loc_el else "Genève"
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
                        "found_at": datetime.now().isoformat(),
                    }
                    jobs.append(finalize(j))
            except Exception as e:
                log(f"Indeed PW [{term}]: {e}")
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
    location = m.group(1).strip() if m else "Genève"
    return title, location


def scrape_jobs_ch_pw() -> list:
    """Offres jobs.ch via Playwright (rendu JS). Best-effort.

    Plus gros board suisse, mais protégé (Cloudflare) et recouvrant largement
    jobscout24 (même groupe JobCloud — la fusion des doublons par titre+employeur
    regroupe les annonces communes). Nécessite un Chromium système.

    NB : jobs.ch fait de la recherche *sémantique* — une requête niche (ex.
    « bibliothécaire ») ramène aussi des offres voisines hors-sujet, écartées
    par is_relevant(). Un total de 0 sur un run = aucune offre Lettres en zone
    ce jour-là, PAS un sélecteur cassé (vérifié : les cartes se parsent bien).
    """
    if not PLAYWRIGHT_AVAILABLE:
        log("jobs.ch : Chromium système introuvable — source ignorée")
        return []
    jobs, seen_urls = [], set()
    with _sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_CHROMIUM_PATH, headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = _new_stealth_context(browser)
        page = ctx.new_page()
        for term in JOBS_CH_QUERIES:
            url = (f"https://www.jobs.ch/fr/offres-emplois/"
                   f"?term={quote(term)}&location={quote('Genève')}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.wait_for_selector('a[href*="/offres-emplois/detail/"]',
                                           timeout=8000)
                except Exception:
                    pass
                soup = BeautifulSoup(page.content(), "lxml")
                # jobs.ch = SPA React : chaque résultat est une carte
                # data-cy="serp-item" contenant un lien data-cy="job-link".
                # Ces hooks de test sont stables ; repli sur les anciennes
                # ancres /offres-emplois/detail/ si jobs.ch les retire.
                # SÉLECTEUR À AJUSTER SI BESOIN.
                cards = soup.select('[data-cy="serp-item"]')
                if not cards:
                    cards = soup.select('a[href*="/offres-emplois/detail/"]')
                for card in cards:
                    a = (card.select_one('a[data-cy="job-link"]')
                         or (card if card.name == "a"
                             else card.select_one('a[href*="/offres-emplois/detail/"]')))
                    if not a or not a.get("href"):
                        continue
                    href = a["href"]
                    if "/offres-emplois/detail/" not in href:
                        continue
                    full_url = ("https://www.jobs.ch" + href
                                if href.startswith("/") else href)
                    if full_url in seen_urls:
                        continue
                    title, location = _parse_jobs_ch_anchor(
                        a.get_text(" ", strip=True))
                    if not title or len(title) < 4:
                        continue
                    if not is_french_text(title):
                        log(f"Rejeté (langue non-FR) : {title[:70]}")
                        continue
                    if not is_relevant(title) or is_fle(title):
                        continue
                    if not in_zone(location):
                        continue
                    seen_urls.add(full_url)
                    j = {
                        "title": title, "company": "", "url": full_url,
                        "source": "jobs.ch", "location": location,
                        "description": "",
                        "found_at": datetime.now().isoformat(),
                    }
                    jobs.append(finalize(j))
            except Exception as e:
                log(f"jobs.ch PW [{term}]: {e}")
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


# Portail emploi enseignant. job.educa.ch est HORS LIGNE depuis 2025 : le
# sous-domaine ne résout plus dans le DNS mondial (NXDOMAIN, vérifié via
# dns.google). Grâce au fail-fast de fetch(), un échec DNS ne coûte plus qu'une
# ligne de log. Dès qu'un portail successeur est connu, repointer ici (1 ligne).
EDUCA_URLS = [
    "https://job.educa.ch/fr/recherche",
    "https://job.educa.ch/fr",
]


def scrape_educa() -> list:
    """Offres d'enseignement via le portail officiel du corps enseignant suisse.

    Source conservée mais actuellement injoignable (voir EDUCA_URLS). Renvoie 0
    proprement et sans pénalité de temps tant qu'aucun successeur n'est repointé.
    """
    jobs, seen_urls = [], set()
    reachable = False
    for url in EDUCA_URLS:
        # job.educa.ch est hors-ligne (NXDOMAIN). Si le host ne résout pas, on
        # saute SANS fetch ni log d'erreur : la source reste en sommeil, prête à
        # reprendre dès que le DNS répond ou qu'on repointe EDUCA_URLS.
        if not host_resolves(url):
            continue
        reachable = True
        soup = fetch(url)
        if not soup:
            continue
        # --- SÉLECTEUR À AJUSTER SI BESOIN ---
        candidates = soup.select(
            "a[href*='/offre'], a[href*='/job'], a[href*='/stelle'], "
            "article a[href], .views-row a[href], li a[href], h2 a[href], h3 a[href]"
        )
        for a in candidates:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if not title or len(title) < 6 or not href:
                continue
            full_url = urljoin("https://job.educa.ch/", href)
            consider(title, full_url,
                     {"company": "École (educa.Job)", "source": "job.educa.ch",
                      "location": "Suisse"}, jobs, seen_urls)
        if jobs:
            break
    # Tant que le portail est injoignable, on reste totalement silencieux.
    if reachable:
        _warn_if_empty("job.educa.ch", jobs)
        log(f"job.educa.ch: {len(jobs)} offre(s) trouvée(s)")
    return jobs


_EDUCH_OFFER_RE = re.compile(r"/emploi/.+-e\d+\.html")
# Le texte du lien educh accole au titre des métadonnées emoji :
# « Titre 📍 Lieu 🕒 Taux 📄 Contrat Employeur ». SÉLECTEUR À AJUSTER SI BESOIN.
_EDUCH_EMOJI = "📍🕒📄💼🗓️"
_EDUCH_SEG_RE = re.compile(rf"([{_EDUCH_EMOJI}])\s*([^{_EDUCH_EMOJI}]*)")
_EDUCH_CONTRACT_RE = re.compile(
    r"^(CDI|CDD|Permanent|Temporaire|Stage|Auxiliaire|Mission|Apprentissage|"
    r"Fixe|Int[ée]rim|Temps\s+partiel|Temps\s+plein)\b\s*", re.I)

# Les listes educh préfixent chaque libellé d'une date relative ou absolue
# (« il y a 2 heures », « 11 juin »…) ; on la retire avant d'isoler le titre.
_EDUCH_MONTHS = ("janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
                 "septembre|octobre|novembre|décembre|decembre")
_EDUCH_DATE_PREFIX_RE = re.compile(
    rf"^\s*(?:il\s+y\s+a\s+\d+\s+(?:minute|heure|jour|semaine|mois|an|année)s?"
    rf"|aujourd['’]hui|hier"
    rf"|\d{{1,2}}\s+(?:{_EDUCH_MONTHS}))\s+",
    re.I)


def _parse_educh_anchor(text: str):
    """Décompose le libellé d'un lien educh en (titre, lieu, taux, employeur).

    L'employeur (segment 📄, après le type de contrat) sert de `company` réelle :
    cela évite l'enrichissement par lecture de page (la page educh est polluée par
    d'autres annonces, ce qui fausserait la pertinence). Champs absents → "".
    """
    text = _EDUCH_DATE_PREFIX_RE.sub("", text.strip())
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


def scrape_educh() -> list:
    """Offres educh.ch (petite enfance, social, enseignement spécialisé) — Genève.

    Le robots.txt d'educh.ch autorise le crawl (User-agent: * sans Disallow, et un
    sitemap d'offres dédié). On lit directement la liste DÉJÀ filtrée par canton
    pour ne garder que Genève sans parcourir les ~300 offres nationales.
    """
    jobs, seen_urls = [], set()
    raw_links = 0
    # Source principale : la page de recherche Genève (fonctionne, déjà ciblée).
    # Les listings /emploi/<canton>/ renvoient une erreur serveur (Smarty) depuis
    # 2026-06 ; on les conserve pour reprise auto si educh les répare. seen_urls
    # dédoublonne et in_zone() (dans consider) assure le filtre géographique.
    for url in ("https://www.educh.ch/recherche/geneve.html",
                "https://www.educh.ch/emploi/geneve-canton/",
                "https://www.educh.ch/emploi/geneve-ville/"):
        soup = fetch(url)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not _EDUCH_OFFER_RE.search(href):
                continue
            raw_links += 1
            if not href.startswith("http"):
                href = urljoin("https://www.educh.ch", href)
            title, location, taux, company = _parse_educh_anchor(
                a.get_text(" ", strip=True))
            fields = {"company": company or "educh.ch", "source": "educh.ch",
                      "location": location or "Genève"}
            if taux:
                fields["taux"] = taux
            consider(title, href, fields, jobs, seen_urls)
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
# Auto-diagnostic de santé des sources (point 4)
# ---------------------------------------------------------------------------

def load_health() -> dict:
    if HEALTH_FILE.exists():
        try:
            return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_health(health: dict):
    HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2),
                           encoding="utf-8")


# Sources connues comme dormantes (domaine hors-ligne / mur anti-bot persistant,
# ou board fonctionnel mais quasi sans offre FR/romande — ex. bibliosuisse).
# On continue de suivre leur santé, mais sans émettre d'alerte de bruit tant
# qu'on ne les a pas réparées/repointées (elles restent dans SCRAPERS).
HEALTH_SILENT_SOURCES = {"educa", "indeed_pw", "jobs_ch_pw", "bibliosuisse"}


def update_health(source: str, count: int, health: dict) -> list:
    """Met à jour l'historique et renvoie des alertes si une source dégénère.

    Alerte si une source qui ramenait >0 en moyenne tombe à 0.
    """
    alerts = []
    entry = health.get(source, {"runs": 0, "total": 0, "last": None, "max": 0})
    avg_before = (entry["total"] / entry["runs"]) if entry["runs"] else 0
    entry["runs"] += 1
    entry["total"] += count
    entry["last"] = count
    entry["max"] = max(entry["max"], count)
    health[source] = entry
    if source in HEALTH_SILENT_SOURCES:
        return alerts          # suivi conservé, mais pas d'alerte (source en sommeil)
    # Détection de panne : la source produisait régulièrement, et tombe à 0
    if count == 0 and entry["max"] >= 1 and avg_before >= 0.5 and entry["runs"] > 2:
        alerts.append(
            f"🚨 {source} : 0 offre alors que la moyenne historique était "
            f"{avg_before:.1f} (max {entry['max']}). Sélecteur probablement cassé."
        )
    # Source chroniquement muette : n'a JAMAIS rien produit malgré plusieurs runs
    elif entry["max"] == 0 and entry["runs"] >= 5:
        alerts.append(
            f"🔇 {source} : 0 offre depuis {entry['runs']} runs (jamais aucun "
            f"résultat) — à déboguer ou repointer."
        )
    return alerts


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
    subject = f"[find_job] {len(new_jobs)} offre(s)"
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
# Rapport HTML (toutes les valeurs dynamiques sont échappées) + tri par score
# ---------------------------------------------------------------------------

def generate_html(new_jobs: list, all_jobs: list):
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")

    def rows(job_list, css_class=""):
        out = ""
        for j in job_list:
            found = escape(j["found_at"][:16].replace("T", " "))
            taux = escape(j.get("taux", "") or "—")
            score = j.get("score", 0)
            out += (
                f'<tr class="{escape(css_class)}">'
                f'<td><a href="{escape(j["url"])}" target="_blank" rel="noopener">'
                f'{escape(j["title"])}</a></td>'
                f'<td>{escape(job_employer(j) or "—")}</td>'
                f'<td>{escape(j.get("location", "—"))}</td>'
                f'<td>{taux}</td>'
                f'<td>{escape(j["source"])}</td>'
                f'<td style="text-align:center">{score}</td>'
                f'<td>{found}</td>'
                f'</tr>\n'
            )
        return out

    header = ('<table><thead><tr><th>Poste</th><th>Entreprise</th><th>Lieu</th>'
              '<th>Taux</th><th>Source</th><th>Score</th><th>Trouvé le</th>'
              '</tr></thead>')

    new_sorted = sorted(new_jobs, key=lambda x: x.get("score", 0), reverse=True)
    section_new = (
        "<p>Aucune nouvelle offre depuis la dernière recherche.</p>"
        if not new_jobs else
        f'{header}<tbody>{rows(new_sorted, "new")}</tbody></table>'
    )

    # Tri principal : date « Trouvé le » décroissante (plus récentes en tête),
    # puis score décroissant en cas d'égalité.
    all_sorted = sorted(all_jobs,
                        key=lambda x: (x["found_at"], x.get("score", 0)),
                        reverse=True)
    section_all = (
        "<p>Aucune offre trouvée.</p>"
        if not all_jobs else
        f'{header}<tbody>{rows(all_sorted)}</tbody></table>'
    )

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offres d'emploi – Lettres Modernes – Genève</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1200px; margin: 2rem auto;
          color: #222; padding: 0 1rem; }}
  h1 {{ color: #1a56db; }}
  h2 {{ margin-top: 2rem; border-bottom: 2px solid #e5e7eb; padding-bottom: .5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; font-size: .95rem; }}
  th {{ background: #1a56db; color: white; padding: .6rem .8rem; text-align: left; }}
  td {{ padding: .5rem .8rem; border-bottom: 1px solid #e5e7eb; }}
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
<p class="updated">Dernière mise à jour : {now} · Tri par score de pertinence</p>

<h2>Nouvelles offres <span class="badge">{len(new_jobs)}</span></h2>
{section_new}

<h2>Toutes les offres ({len(all_jobs)})</h2>
{section_all}
</body>
</html>"""

    RESULTS_FILE.write_text(html, encoding="utf-8")
    PUBLIC_FILE.write_text(html, encoding="utf-8")
    log(f"Rapport HTML mis à jour : {RESULTS_FILE}")


def generate_rss(all_jobs: list):
    """Génère un flux RSS des offres (bonus, lisible en agrégateur/mobile)."""
    recent = sorted(all_jobs, key=lambda x: x["found_at"], reverse=True)[:50]
    items = ""
    for j in recent:
        title = escape(j["title"])
        link = escape(j["url"])
        src = escape(j.get("source", ""))
        loc = escape(j.get("location", ""))
        try:
            pub_dt = datetime.fromisoformat(j["found_at"])
        except (KeyError, ValueError):
            pub_dt = datetime.now()
        pub_date = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
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
        "<title>Offres Lettres Modernes – Genève</title>"
        "<link>https://gabigbarig.github.io/find_job/</link>"
        "<description>Veille d'offres d'emploi</description>\n"
        f"{items}"
        "</channel></rss>"
    )
    RSS_FILE.write_text(rss, encoding="utf-8")


# ---------------------------------------------------------------------------
# Persistance des offres
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
    # Public / para-public
    scrape_ville_geneve, scrape_ge_ch, scrape_vaud,
    # Universités & recherche (NOUVEAU)
    scrape_unige, scrape_myscience,
    # Culture / enseignement spécialisés (NOUVEAU)
    scrape_museums, scrape_educa, scrape_educh, scrape_bibliosuisse,
    # Presse / privé
    scrape_letemps, scrape_jobscout24, scrape_jobup,
    # Agrégateurs
    scrape_indeed_pw, scrape_adzuna, scrape_jobs_ch_pw,
]

# Sources rendues via Playwright : exécutées en séquence dans un seul thread (l'API
# sync de Playwright ne supporte pas le parallélisme multi-thread), en parallèle du
# pool HTTP. Voir la parallélisation dans main().
PLAYWRIGHT_SCRAPERS = (scrape_myscience, scrape_indeed_pw, scrape_jobs_ch_pw)


def main():
    log("=== Démarrage de la recherche d'emploi ===")
    seen = load_seen()
    all_jobs = load_all_jobs()
    health = load_health()
    all_jobs, seen = expire_old_jobs(all_jobs, seen)
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_french_text(j.get("title", ""))]
    removed = before - len(all_jobs)
    if removed:
        log(f"Nettoyage : {removed} offre(s) non-francophone(s) retirée(s) de l'archive")

    # Ré-validation de l'archive avec les filtres ACTUELS (pertinence, zone, score).
    # Purge les entrées captées sous d'anciennes règles (FLE, labo, hors-zone…).
    # On recalcule le score car les listes de mots-clés ont pu changer.
    before = len(all_jobs)
    revalidated = []
    for j in all_jobs:
        j["score"] = relevance_score(j["title"], j.get("description", ""))
        if passes_filters(j):
            revalidated.append(j)
    all_jobs = revalidated
    purged = before - len(all_jobs)
    if purged:
        log(f"Ré-validation : {purged} offre(s) archivée(s) désormais hors critères retirée(s)")

    # Purge des liens morts : une offre dont la page renvoie 404/410 a été retirée
    # par la source et ne doit plus figurer dans le rapport (lien cassé).
    # Parallélisé (requêtes HEAD indépendantes) pour ne pas allonger le run.
    before = len(all_jobs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        dead_flags = list(pool.map(lambda j: url_is_dead(j["url"]), all_jobs))
    all_jobs = [j for j, is_dead in zip(all_jobs, dead_flags) if not is_dead]
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

    def _run_one(scraper):
        """Exécute un scraper isolément → [(nom, résultats)] ; n'émet jamais d'exception."""
        name = scraper.__name__.replace("scrape_", "")
        try:
            return [(name, scraper())]
        except Exception as e:
            log(f"⚠️  {scraper.__name__} a échoué : {e}")
            return [(name, [])]

    def _run_playwright_group():
        """Sources Playwright en SÉQUENCE (l'API sync ne tolère pas le multi-thread)."""
        out = []
        for scraper in PLAYWRIGHT_SCRAPERS:
            out.extend(_run_one(scraper))
        return out

    # Scrapers HTTP en parallèle (I/O-bound) + groupe Playwright dans une tâche unique
    # lancée en parallèle. La politesse par domaine est garantie par _POLITE_LOCK.
    http_scrapers = [s for s in SCRAPERS if s not in PLAYWRIGHT_SCRAPERS]
    collected = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_run_one, s) for s in http_scrapers]
        futures.append(pool.submit(_run_playwright_group))
        for fut in as_completed(futures):
            collected.extend(fut.result())

    # Agrégation SÉQUENTIELLE dans le thread principal (pas de race sur raw/health).
    for source_name, results in collected:
        raw.extend(results)
        health_alerts.extend(update_health(source_name, len(results), health))

    # Nouvelles offres : enrichissement employeur + même gate de pertinence.
    new_jobs = []
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
        if not passes_filters(job):
            continue
        seen.add(jid)
        job["_new"] = True
        new_jobs.append(job)

    # Fusion des doublons sur (archive + nouvelles offres). On traite les offres
    # à employeur CONNU d'abord (canoniques, absorbent les variantes sans
    # employeur), puis par ordre d'ancienneté. Deux écoles distinctes au même
    # intitulé restent séparées (employeurs connus différents).
    combined = sorted(
        all_jobs + new_jobs,
        key=lambda j: (0 if job_employer(j) else 1, j.get("found_at", "")),
    )
    fp_to_known: dict = {}
    deduped = [j for j in combined if not is_duplicate(j, fp_to_known)]
    merged = len(combined) - len(deduped)
    if merged:
        log(f"Fusion doublons : {merged} offre(s) en double regroupée(s)")
    all_jobs = deduped
    new_jobs = [j for j in deduped if j.pop("_new", False)]

    log(f"Nouvelles offres : {len(new_jobs)} | Total cumulé : {len(all_jobs)} "
        f"| Pages de détail lues : {_detail_fetch_count} "
        f"| Pages employeur lues : {_employer_fetch_count}")
    for alert in health_alerts:
        log(alert)

    save_seen(seen)
    save_all_jobs(all_jobs)
    save_health(health)
    generate_html(new_jobs, all_jobs)
    generate_rss(all_jobs)
    send_alert(new_jobs, health_alerts)
    log("=== Recherche terminée ===\n")


if __name__ == "__main__":
    main()