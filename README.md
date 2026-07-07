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

Sources supplémentaires du profil `systemes` : SwissDevJobs, itjobs.ch,
ITBoard, CERN, CICR, OMPI, Job-Room, SIG, TPG, ONU Genève et OMC. Job-Room,
TPG et ONU Genève nécessitent Chromium/Playwright.

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
08:17, 14:17 et 20:17, heure de Genève, puis publie les changements de `docs/`.
Il peut aussi être lancé manuellement depuis l'onglet **Actions**. L'historique
contenu dans `data/` est conservé dans un cache GitHub sans être ajouté au dépôt.

Dans **Settings → Secrets and variables → Actions**, ajouter si nécessaire :

- `ADZUNA_ID` et `ADZUNA_KEY` ;
- `SMTP_FROM`, `SMTP_PASS` et `SMTP_TO` ;
- les cinq secrets `LINKEDIN_IMAP_*` présentés ci-dessus.

Les identifiants sont tous facultatifs : les sources concernées sont simplement
ignorées quand leurs secrets ne sont pas configurés.

Les rapports GitHub Pages partagent une interface responsive avec recherche,
filtres et tri. Les favoris, candidatures envoyées et offres masquées sont
conservés dans le navigateur ; l'export/import permet de transférer ce suivi.
Les correspondances locales trop faibles pour la sélection principale sont
conservées dans « Offres à vérifier ». La page `docs/status.html` expose la
couverture des sources, les requêtes muettes et les motifs de rejet.
