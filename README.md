En résumé, ton flux de tous les jours :

Ouvre VS Code
Ctrl+R → choisis find_job [WSL: Ubuntu]

ou bien

Ouvre un terminal Ubuntu (wsl)
cd ~/git_projects/find_job && code .

Profils disponibles :

```bash
./run.sh                  # lance lettres + comptabilité + systèmes/Linux
./run.sh lettres          # lance seulement Lettres modernes
./run.sh comptabilite     # lance seulement comptabilité
./run.sh systemes         # lance seulement systèmes/Linux
./view.sh                 # lance tout et ouvre docs/index.html
./view.sh comptabilite    # lance comptabilité et ouvre son rapport local
./view.sh systemes        # lance systèmes/Linux et ouvre son rapport local
./publish.sh              # publie les trois rapports GitHub Pages
```

Sources supplémentaires : ReliefWeb, CAGI et cinfoPoste pour la Genève
internationale ; CERN, CICR, OMPI, SIG, TPG, ONU, OMC, OMS, OIT, UIT, UNICEF,
HUG, Ville de Nyon, Ecolint et Pictet sont aussi lus puis filtrés par métier.
Sources réservées au profil `systemes` : SwissDevJobs, itjobs.ch, ITBoard et
Job-Room. Job-Room, TPG, Pictet et certaines pages institutionnelles utilisent
Chromium/Playwright lorsque leur HTML direct ne suffit pas. CAGI l'utilise
seulement en repli si son listing HTML n'est pas accessible directement.

Ubuntu 26.04 nécessite Playwright 1.61 ou plus récent. Mettre à niveau la
bibliothèque, puis installer une fois son navigateur compatible :

```bash
./venv/bin/python3 -m pip install --upgrade "playwright>=1.61,<2"
./venv/bin/python3 -m playwright install --with-deps chromium
```

Sous WSL, le lanceur Ubuntu `/usr/bin/chromium-browser` peut rediriger vers un
paquet Snap qui ne fonctionne pas dans cet environnement. Le scraper l'ignore
donc et privilégie le navigateur géré par Playwright. Un Chrome/Chromium natif
peut aussi être indiqué avec `CHROMIUM_EXECUTABLE_PATH=/chemin/vers/chrome`.

Le profil Lettres garde une sélection principale stricte Genève + Nyon proche.
Les offres au métier pertinent mais au lieu incertain vont dans « Offres à
vérifier ». Un lieu explicitement hors zone (autre canton, ville ou pays) est
rejeté ; seuls les lieux réellement inconnus restent à confirmer. Un rappel
élargi hebdomadaire ajoute aussi Lausanne/Gland/Rolle/Morges en revue uniquement.
Il est actif le dimanche par défaut (`BROAD_RECALL_DAYS=6`), forçable avec
`BROAD_RECALL=1` ou désactivable avec `BROAD_RECALL=0`.

ReliefWeb exige depuis le 1er novembre 2025 un nom d'application pré-approuvé.
La source reste donc désactivée tant que `RELIEFWEB_APPNAME` n'est pas défini
avec une valeur approuvée par ReliefWeb ; le scraper ne tente pas de valeur
factice qui produirait des erreurs HTTP 403.

Les pages de détail sont enrichies après la collecte, en priorité lorsqu'elles
permettent de confirmer le métier ou le lieu. Les fiches déjà lues sont gardées
48 heures par défaut (`DETAIL_CACHE_TTL_HOURS`) et le cache est limité à 1 200
entrées (`MAX_DETAIL_CACHE_ENTRIES`). Les liens archivés ne sont revérifiés que
toutes les 24 heures par défaut (`DEAD_LINK_CHECK_TTL_HOURS`) afin d'éviter les
requêtes inutiles. Les fichiers d'état sont écrits atomiquement et leur version
précédente est conservée en secours.

Un verrou commun empêche deux recherches d'écrire simultanément dans les
rapports, y compris lors d'un lancement direct avec `python3 scraper.py`. Une
seconde exécution est ignorée proprement pendant que la première travaille.

LinkedIn n'est pas scanné : la plateforme interdit explicitement le scraping
automatisé. Deux alternatives sont intégrées :

- lecture facultative des alertes emploi LinkedIn reçues dans une boîte IMAP ;
- lecture directe des portails ATS publics (Workday et SmartRecruiters) déclarés
  dans `ats_sources.json`.

Le lecteur d'alertes ne télécharge jamais les fiches LinkedIn et ne marque pas
les messages comme lus. Pour l'activer, définir les variables suivantes dans
`.env` en local ou dans les secrets GitHub Actions :

```text
LINKEDIN_IMAP_HOST=imap.gmail.com
LINKEDIN_IMAP_PORT=993
LINKEDIN_IMAP_USER=adresse@example.com
LINKEDIN_IMAP_PASS=mot_de_passe_application
LINKEDIN_IMAP_FOLDER=INBOX
```

`LINKEDIN_ALERT_DEFAULT_LOCATION=Genève` est facultatif. Ne l'utiliser que si
la recherche enregistrée LinkedIn est elle-même strictement limitée à Genève ;
sinon une offre sans lieu explicite pourrait être classée à tort. Sur Gmail, le
mot de passe doit être un mot de passe d'application, pas celui du compte.

## Automatisation GitHub

Le workflow `.github/workflows/recherche-emploi.yml` lance les trois profils à
08:17, 14:17 et 20:17, heure de Genève. Ils s'exécutent en parallèle avec un
cache d'historique distinct, puis un quatrième job fusionne et publie les trois
rapports. Il peut aussi être lancé manuellement depuis l'onglet **Actions**.
L'historique contenu dans `data/` reste dans les caches GitHub sans être ajouté
au dépôt. Un instantané récupérable est également gardé 14 jours dans les
artefacts de chaque exécution.

Dans **Settings → Secrets and variables → Actions**, ajouter si nécessaire :

- `ADZUNA_ID` et `ADZUNA_KEY` ;
- `RELIEFWEB_APPNAME` uniquement après approbation par ReliefWeb ;
- `SMTP_FROM`, `SMTP_PASS` et `SMTP_TO` ;
- les cinq secrets `LINKEDIN_IMAP_*` présentés ci-dessus.

Les identifiants sont tous facultatifs : les sources concernées sont simplement
ignorées quand leurs secrets ne sont pas configurés.

Les rapports GitHub Pages partagent une interface responsive avec recherche,
filtres et tri. Les favoris, candidatures envoyées et offres masquées sont
conservés dans le navigateur ; l'export/import permet de transférer ce suivi.
Les correspondances locales trop faibles pour la sélection principale sont
conservées dans « Offres à vérifier ». La page `docs/status.html` expose la
couverture des sources, leur état, leur durée, leur dernière exécution réellement
saine et leur production à chaque étape (brut, unique, sélection, revue). Elle
avertit aussi si la publication devient ancienne et affiche les requêtes muettes
et les motifs de rejet. Les portails ATS configurés sont suivis individuellement
afin qu'une panne ne soit pas masquée par les autres. Les principaux sélecteurs
fragiles sont vérifiés hors réseau à partir des fixtures de `tests/fixtures/`.
