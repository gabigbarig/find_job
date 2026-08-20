import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import scraper


FIXTURES = Path(__file__).parent / "tests" / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name):
    return json.loads(fixture_text(name))


def classify(profile, title, description="", location="Genève"):
    scraper.configure_profile(profile)
    job = scraper.finalize({
        "title": title,
        "description": description,
        "location": location,
        "url": "https://example.test/job",
        "source": "test",
    })
    if scraper.passes_filters(job):
        return "main"
    keep, _ = scraper.review_candidate(job)
    return "review" if keep else "reject"


class RelevanceRegressionTests(unittest.TestCase):
    def test_systemes_rejects_unrelated_titles(self):
        rejected = [
            "Assistante Administrative Japonaise // Japanese Administrative Assistant",
            "Electrical Engineer for Safety Systems",
            "Electrical Cabling Project Technician",
            "Mechanical Technician",
            "Technical Engineer for Water Treatment Systems",
            "Trust Administrator",
            "UN TECHNICIEN METHODE ET INDUSTRIALISATION",
            "Software Platform Engineer",
            "Embedded Linux Development Engineer",
        ]
        for title in rejected:
            with self.subTest(title=title):
                self.assertEqual(classify("systemes", title), "reject")

    def test_systemes_keeps_infrastructure_roles(self):
        accepted = [
            "IT Server Administrator",
            "Ingénieur Système Linux",
            "Kubernetes Tech Lead",
            "Platform Engineering Lead",
            "Expert Technique Cloud & Platform Engineering",
        ]
        for title in accepted:
            with self.subTest(title=title):
                self.assertEqual(classify("systemes", title), "main")

    def test_lettres_rejects_other_subjects_and_generic_admin(self):
        rejected = [
            "Replacement Primary English teacher",
            "Maîtresse ou maître d'enseignement général - mathématiques",
            "Maîtresse ou maître d'enseignement général / Italien",
            "Assistant-e administratif-ve 1",
            "Assistant Socio Educatif",
            "Assistante de Direction",
            "Technical Studentship (General and Civil Engineering)",
            "Content Specialist (m/w) - 100%",
            "Creative Content Specialist",
            "Retail Watchmaking Training Content Specialist",
            "Records Manager",
            "AI Marketing Video Editor",
        ]
        for title in rejected:
            with self.subTest(title=title):
                self.assertEqual(classify("lettres", title), "reject")

    def test_lettres_keeps_explicit_roles(self):
        accepted = [
            "Agente en information documentaire",
            "Bibliothécaire documentaliste",
            "Maître d'enseignement général - Français",
            "Communications Manager",
            "CERN Courier Editor",
        ]
        for title in accepted:
            with self.subTest(title=title):
                self.assertEqual(classify("lettres", title), "main")

    def test_description_only_requires_two_strong_anchors(self):
        self.assertEqual(
            classify("lettres", "Coordinateur de programme", "Gestion documentaire."),
            "reject",
        )
        self.assertEqual(
            classify(
                "lettres",
                "Coordinateur de programme",
                "Gestion documentaire, records management et knowledge management.",
            ),
            "review",
        )

    def test_lettres_keeps_strong_unknown_location_for_review(self):
        self.assertEqual(
            classify("lettres", "Bibliothécaire documentaliste", location=""),
            "review",
        )

    def test_explicit_far_locations_are_never_sent_to_review(self):
        for location in (
            "Shanghai, Chine",
            "Singapore",
            "Rabat, Maroc",
            "Lille, France",
            "Spreitenbach, Aargau, Switzerland",
        ):
            with self.subTest(location=location):
                self.assertEqual(
                    classify("lettres", "Communications Manager",
                             location=location),
                    "reject",
                )

    def test_structured_geography_exposes_country_canton_and_city(self):
        local = scraper.structured_geography("Meyrin, Genève, Suisse")
        self.assertEqual(local["status"], "target")
        self.assertEqual(local["canton"], "Genève")
        self.assertEqual(local["city"], "meyrin")

        foreign = scraper.structured_geography("Rabat, Maroc")
        self.assertEqual(foreign["status"], "outside")
        self.assertEqual(foreign["country"], "maroc")
        self.assertEqual(foreign["city"], "rabat")

        swiss_far = scraper.structured_geography(
            "Spreitenbach, Aargau, Switzerland"
        )
        self.assertEqual(swiss_far["country"], "Suisse")
        self.assertEqual(swiss_far["canton"], "Argovie")
        self.assertEqual(swiss_far["city"], "spreitenbach")

    def test_geneva_neighborhoods_and_postcodes_are_local(self):
        expected = {
            "1219 Le Lignon": "Genève",
            "Châtelaine": "Genève",
            "CH-GE": "Genève",
            "1260 Nyon": "Vaud",
        }
        for location, canton in expected.items():
            with self.subTest(location=location):
                geography = scraper.structured_geography(location)
                self.assertEqual(geography["status"], "target")
                self.assertEqual(geography["canton"], canton)

    def test_final_foreign_iso_country_code_is_outside(self):
        geography = scraper.structured_geography("Houston, TX, us")
        self.assertEqual(geography["status"], "outside")
        self.assertEqual(geography["country"], "États-Unis")
        self.assertEqual(
            classify(
                "comptabilite",
                "Project Accounting Analyst",
                location="Houston, TX, us",
            ),
            "reject",
        )

    def test_local_location_wins_over_foreign_mentions_in_description(self):
        self.assertEqual(
            classify(
                "lettres",
                "Communications Manager",
                "Coordination régulière avec les bureaux de Singapore et Rabat.",
                location="Genève",
            ),
            "main",
        )

    def test_labeled_far_location_in_description_is_rejected(self):
        self.assertEqual(
            classify(
                "lettres", "Communications Manager",
                "Duty Station: Shanghai. Coordination éditoriale.",
                location="",
            ),
            "reject",
        )

    def test_mixed_local_and_foreign_location_is_not_assumed_local(self):
        self.assertEqual(
            classify(
                "lettres", "Communications Manager",
                location="Genève / Singapore",
            ),
            "reject",
        )

    def test_foreign_city_in_title_is_rejected_even_with_local_metadata(self):
        self.assertEqual(
            classify(
                "lettres", "Communications Manager Singapore",
                location="Genève",
            ),
            "reject",
        )

    def test_consider_can_use_trusted_search_geography(self):
        scraper.configure_profile("lettres")
        previous = scraper.FETCH_LOCAL_DETAILS
        scraper.FETCH_LOCAL_DETAILS = False
        try:
            jobs = []
            scraper.consider(
                "Communications Manager",
                "https://example.test/job/1",
                {"company": "Organisation test", "source": "test",
                 "location": "", "_trusted_geo": True, "_no_fetch": True},
                jobs,
                set(),
            )
        finally:
            scraper.FETCH_LOCAL_DETAILS = previous
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["location"], "Genève")

    def test_json_ld_job_fields_extract_jobposting_metadata(self):
        soup = scraper.BeautifulSoup(
            """
            <script type="application/ld+json">
            {"@context":"https://schema.org","@type":"JobPosting",
             "title":"Communications Manager",
             "description":"Rédaction de contenu et publications.",
             "hiringOrganization":{"name":"Organisation test"},
             "datePosted":"2026-08-01",
             "validThrough":"2026-09-01T23:59:59+02:00",
             "employmentType":"FULL_TIME",
             "jobLocationType":"TELECOMMUTE",
             "identifier":{"@type":"PropertyValue","value":"REQ-42"},
             "baseSalary":{"currency":"CHF","value":{"minValue":80000,
              "maxValue":100000,"unitText":"YEAR"}},
             "jobLocation":{"address":{"addressLocality":"Geneva",
              "addressCountry":"Switzerland"}}}
            </script>
            """,
            "lxml",
        )
        fields = scraper._json_ld_job_fields(soup)
        self.assertEqual(fields["company"], "Organisation test")
        self.assertIn("Geneva", fields["location"])
        self.assertIn("Rédaction", fields["description"])
        self.assertEqual(fields["date_posted"], "2026-08-01")
        self.assertEqual(fields["valid_through"], "2026-09-01T23:59:59+02:00")
        self.assertEqual(fields["employment_type"], "FULL_TIME")
        self.assertEqual(fields["job_location_type"], "TELECOMMUTE")
        self.assertEqual(fields["external_id"], "REQ-42")
        self.assertEqual(fields["salary"], "80 000–100 000 CHF/an")
        self.assertEqual(scraper.job_contract(fields), "Temps plein")
        self.assertEqual(scraper.job_work_mode(fields), "Télétravail")
        self.assertEqual(scraper.job_posted_date(fields), "01.08.2026")
        self.assertEqual(scraper.job_deadline(fields), "01.09.2026")

    def test_comptabilite_rejects_commercial_and_software_roles(self):
        for title in (
            "Key Account Manager",
            "Backend Software Engineer (Facturation)",
            "Assistant de Direction auprès du Directeur Financier",
            "NetSuite & Payroll Systems Administrator",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify("comptabilite", title), "reject")
        self.assertEqual(classify("comptabilite", "Regional Financial Controller"), "main")
        self.assertEqual(classify("comptabilite", "Directeur Administratif et Financier"), "main")

    def test_description_noise_is_removed(self):
        polluted = (
            "Régions Choisissez une région Plus d'offres d'emploi: chauffeur "
            "20 emploi(s) similaire(s) trouvé(s) Administrateur Système Genève"
        )
        self.assertEqual(scraper.sanitize_description(polluted, "Administrateur Système"), "")
        clean = (
            "Administrateur Système Vous gérez Linux et Windows Server. "
            "Autres recherches d'emplois développeur et comptable"
        )
        self.assertEqual(
            scraper.sanitize_description(clean, "Administrateur Système"),
            "Administrateur Système Vous gérez Linux et Windows Server.",
        )

    def test_title_cleanup_and_german_detection(self):
        self.assertEqual(
            scraper.clean_job_title("Il y a 3 trimestres Un (e) Comptable junior"),
            "Un (e) Comptable junior",
        )
        self.assertFalse(scraper.is_french_text("Senior Site Reliability Engineer Hosting Plattformen"))

    def test_german_description_is_rejected_even_with_english_title(self):
        german_description = (
            "Als Teil unseres Teams gestalten Sie die globale Kommunikation. "
            "Ihre Aufgaben umfassen die Konzeption und Umsetzung verschiedener "
            "Massnahmen. Sie arbeiten mit unseren Fachpersonen und bringen "
            "mehrjahrige Berufserfahrung sowie sehr gute Deutschkenntnisse mit."
        )
        for profile, title in (
            ("lettres", "Product Communication Manager (all genders)"),
            ("systemes", "Senior IT System Engineer"),
            ("comptabilite", "Senior Accountant"),
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    classify(profile, title, german_description),
                    "reject",
                )

    def test_french_and_bilingual_offers_are_kept(self):
        french_description = (
            "Au sein de notre équipe, vous assurez les missions de communication. "
            "Votre profil et vos compétences correspondent au poste. "
            "La maîtrise de l'allemand constitue un atout."
        )
        self.assertEqual(
            classify("lettres", "Communications Manager", french_description),
            "main",
        )
        self.assertTrue(scraper.is_french_text(
            "Un chargé de communication *** Kommunikationsbeauftragter oder "
            "Kommunikationsbeauftragte"
        ))


class LinkedInAlertTests(unittest.TestCase):
    def test_extracts_job_from_email_without_tracking_parameters(self):
        message = EmailMessage()
        message["From"] = "LinkedIn Jobs <jobs-noreply@linkedin.com>"
        message["To"] = "candidate@example.test"
        message["Subject"] = "Nouvelles offres : ingénieur système"
        message.set_content("Cette alerte contient une version HTML.")
        message.add_alternative(
            """
            <html><body><div class="job-card">
              <a href="https://www.linkedin.com/jobs/view/ingenieur-linux-1234567890/?trk=email">
                Ingénieur Système Linux
              </a>
              <div>Entreprise Exemple</div><div>Genève, Suisse</div>
            </div></body></html>
            """,
            subtype="html",
        )

        offers = scraper.parse_linkedin_alert_message(message.as_bytes())

        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["title"], "Ingénieur Système Linux")
        self.assertEqual(offers[0]["company"], "Entreprise Exemple")
        self.assertEqual(offers[0]["location"], "Genève, Suisse")
        self.assertEqual(
            offers[0]["url"], "https://www.linkedin.com/jobs/view/1234567890/"
        )

    def test_ignores_non_linkedin_sender_and_external_url(self):
        message = EmailMessage()
        message["From"] = "Alerte <jobs@example.test>"
        message["Subject"] = "Alerte emploi"
        message.set_content("https://www.linkedin.com/jobs/view/1234567890/")
        self.assertEqual(scraper.parse_linkedin_alert_message(message.as_bytes()), [])
        self.assertEqual(
            scraper._canonical_linkedin_job_url(
                "https://example.test/jobs/view/1234567890/"
            ),
            "",
        )


class DirectATSTests(unittest.TestCase):
    def setUp(self):
        scraper.configure_profile("systemes")
        self.fetch_local_details = scraper.FETCH_LOCAL_DETAILS
        scraper.FETCH_LOCAL_DETAILS = False

    def tearDown(self):
        scraper.FETCH_LOCAL_DETAILS = self.fetch_local_details

    @patch.object(scraper, "_polite_wait")
    @patch.object(scraper, "session")
    def test_workday_adapter_uses_location_and_public_url(self, session, _wait):
        response = Mock()
        response.json.return_value = fixture_json("workday.json")
        session.return_value.post.return_value = response
        jobs = []

        scraper._scrape_workday_source({
            "name": "Employeur Test",
            "source": "ATS test",
            "host": "https://tenant.example.test",
            "tenant": "tenant",
            "site": "careers",
            "public_base": "https://tenant.example.test/fr-FR/careers",
        }, jobs, set())

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["location"], "Genève, Suisse")
        self.assertEqual(
            jobs[0]["url"],
            "https://tenant.example.test/fr-FR/careers/job/Geneva/Linux_R001",
        )
        self.assertEqual(jobs[0]["external_id"], "Linux_R001")
        request = session.return_value.post.call_args
        self.assertEqual(request.kwargs["json"]["limit"], 20)

    @patch.object(scraper, "_polite_wait")
    @patch.object(scraper, "session")
    def test_smartrecruiters_adapter_keeps_only_local_relevant_job(
        self, session, _wait
    ):
        response = Mock()
        response.json.return_value = fixture_json("smartrecruiters.json")
        session.return_value.get.return_value = response
        jobs = []

        scraper._scrape_smartrecruiters_source({
            "name": "Employeur Test", "source": "ATS test",
            "company_id": "Test",
        }, jobs, set())

        self.assertEqual([job["url"] for job in jobs], [
            "https://jobs.smartrecruiters.com/Test/1-system-administrator-linux"
        ])
        self.assertEqual(jobs[0]["external_id"], "1")


class ParserFixtureTests(unittest.TestCase):
    def test_jobscout24_fixture(self):
        offers = scraper._parse_jobscout24_page(
            fixture_text("jobscout24.html"),
            "https://www.jobscout24.ch",
            fallback_location="Genève",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["title"], "Administrateur Système Linux")
        self.assertEqual(offers[0]["location"], "Genève")

    def test_jobup_fixture_and_zone_filter(self):
        offers = scraper._parse_jobup_page(
            fixture_text("jobup.html"),
            "https://www.jobup.ch",
            zone_filter=scraper.GENEVE_ZONE,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["title"], "Bibliothécaire documentaliste")
        self.assertEqual(offers[0]["location"], "Genève")

    def test_jobs_ch_fixture(self):
        offers = scraper._parse_jobs_ch_page(fixture_text("jobs_ch.html"))
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["title"], "Administrateur Système Linux")
        self.assertEqual(offers[0]["location"], "Meyrin")

    def test_educh_fixture(self):
        offers = scraper._parse_educh_page(fixture_text("educh.html"))
        self.assertEqual(len(offers), 2)
        self.assertEqual(offers[0]["title"], "Enseignant de français")
        self.assertEqual(offers[0]["location"], "Genève")
        self.assertEqual(offers[0]["taux"], "80%")
        self.assertEqual(offers[0]["company"], "Gymnase de Candolle")
        self.assertEqual(
            offers[1]["title"], "Enseignant(e) français cycle"
        )
        self.assertEqual(
            offers[1]["url"],
            "https://www.educh.ch/emploi/enseignant-e-francais-cycle-e89629",
        )
        self.assertEqual(offers[1]["taux"], "70%")
        self.assertEqual(offers[1]["company"], "Ecole Ohalei Menahem Habad")

    def test_generic_card_fixture(self):
        soup = scraper.BeautifulSoup(fixture_text("generic_jobs.html"), "lxml")
        offers = list(scraper._generic_job_cards_from_links(
            soup, "https://example.test"
        ))
        self.assertEqual(len(offers), 1)
        title, url, card, card_text = offers[0]
        self.assertEqual(title, "Information Management Officer")
        self.assertEqual(url, "https://example.test/jobs/information-management-42")
        self.assertIn("Organisation Exemple", card_text)
        self.assertEqual(
            scraper._company_from_card(card), "Organisation Exemple"
        )

    def test_wipo_unified_listing_fixture(self):
        offers = scraper._parse_wipo_listing(
            fixture_text("wipo.html"),
            "https://wipo.taleo.net/careersection/wp_1/moresearch.ftl?lang=en",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(
            offers[0]["title"], "CRM and Business Process Assistant"
        )
        self.assertIn("job=26203-TA", offers[0]["url"])
        self.assertEqual(offers[0]["location"], "Genève")

    def test_cagi_listing_fixture_ignores_categories_and_pagination(self):
        offers = scraper._parse_cagi_page(
            fixture_text("cagi.html"),
            "https://jobs.cagi.ch/fr/offres-demploi/",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["title"], "Digital Communications Officer")
        self.assertEqual(offers[0]["location"], "Genève")
        self.assertEqual(offers[0]["company"], "Organisation Exemple")


class ReliabilityTests(unittest.TestCase):
    def test_direct_runs_share_a_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "scraper.lock"
            with patch.object(scraper, "SCRAPER_LOCK_FILE", lock_path), \
                    patch.dict(
                        scraper.os.environ,
                        {"FIND_JOB_LOCK_HELD": ""},
                    ):
                with scraper.scraper_process_lock():
                    with self.assertRaises(scraper.ScraperAlreadyRunning):
                        with scraper.scraper_process_lock():
                            self.fail("Le second verrou ne doit pas être acquis")

    def test_legacy_dates_are_interpreted_in_geneva_timezone(self):
        legacy = scraper.parse_local_datetime("2026-08-07T12:00:00")
        aware = scraper.parse_local_datetime("2026-08-07T10:00:00+00:00")
        self.assertEqual(getattr(legacy.tzinfo, "key", ""), "Europe/Zurich")
        self.assertEqual(aware.hour, 12)
        self.assertEqual(getattr(aware.tzinfo, "key", ""), "Europe/Zurich")

    def test_shared_run_cache_fetches_an_identical_page_once(self):
        response = Mock()
        response.text = (
            "<html><body><main><a href='/job'>Une offre suffisamment "
            "détaillée pour ne pas ressembler à une page vide.</a></main></body></html>"
        )
        response.raise_for_status.return_value = None
        http_session = Mock()
        http_session.get.return_value = response

        with patch.object(scraper, "session", return_value=http_session), \
                patch.object(scraper, "robots_allows", return_value=True), \
                patch.object(scraper, "_polite_wait"):
            with scraper.shared_run_cache():
                first = scraper.fetch("https://example.test/jobs")
                first.select_one("a").decompose()
                second = scraper.fetch("https://example.test/jobs")

        self.assertIsNotNone(second.select_one("a"))
        http_session.get.assert_called_once()

    def test_dead_link_checks_obey_the_configured_ttl(self):
        now = scraper.local_now()
        with patch.object(scraper, "DEAD_LINK_CHECK_TTL_HOURS", 24):
            self.assertTrue(scraper.dead_link_check_due({}))
            self.assertFalse(scraper.dead_link_check_due({
                "url_checked_at": (now - scraper.timedelta(hours=23)).isoformat(),
            }))
            self.assertTrue(scraper.dead_link_check_due({
                "url_checked_at": (now - scraper.timedelta(hours=25)).isoformat(),
            }))

    def test_valid_through_expires_an_offer_even_if_recently_found(self):
        now = scraper.local_now()
        job = {
            "title": "Communications Manager",
            "url": "https://example.test/job/expired",
            "found_at": now.isoformat(),
            "valid_through": (now - scraper.timedelta(days=1)).date().isoformat(),
        }
        identifier = scraper.job_id(job["title"], job["url"])
        with patch.object(scraper, "log"):
            fresh, seen = scraper.expire_old_jobs([job], {identifier})
        self.assertEqual(fresh, [])
        self.assertEqual(seen, set())

    def test_source_registry_drives_all_compatibility_lists(self):
        names = [spec.name for spec in scraper.SOURCE_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            scraper.SCRAPERS,
            [spec.scraper for spec in scraper.SOURCE_SPECS],
        )
        self.assertEqual(
            scraper.SCRAPER_SOURCE_FIELDS,
            {spec.name: spec.source_field for spec in scraper.SOURCE_SPECS},
        )
        system_only = {
            spec.scraper for spec in scraper.SOURCE_SPECS
            if spec.profiles == scraper.SYSTEMES_PROFILE
        }
        self.assertEqual(set(scraper.SYSTEMES_ONLY_SCRAPERS), system_only)
        self.assertTrue(all(
            spec.enabled_for("systemes")
            for spec in scraper.SOURCE_SPECS
        ))

    def test_snap_chromium_wrapper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "chromium-browser"
            wrapper.write_text(
                '#!/bin/sh\nexec /snap/bin/chromium "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            self.assertTrue(scraper._is_snap_chromium(str(wrapper)))

            native = Path(tmp) / "chromium"
            native.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            native.chmod(0o755)
            self.assertFalse(scraper._is_snap_chromium(str(native)))

            alias = Path(tmp) / "snap-alias"
            alias.symlink_to("/snap/chromium/current/chrome")
            self.assertTrue(scraper._is_snap_chromium(str(alias)))

    def test_launch_chromium_prefers_playwright_managed_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp) / "chrome"
            managed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            managed.chmod(0o755)
            browser_type = Mock()
            browser_type.executable_path = str(managed)
            browser_type.launch.return_value = object()
            playwright = Mock(chromium=browser_type)

            with patch.object(scraper, "_CHROMIUM_PATH", "/native/chrome"), \
                    patch.dict(
                        scraper.os.environ,
                        {"CHROMIUM_EXECUTABLE_PATH": ""},
                    ):
                scraper._launch_chromium(playwright)

            browser_type.launch.assert_called_once_with(
                headless=True, executable_path=str(managed)
            )

    def test_find_system_chromium_skips_snap_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "chromium"
            snap.write_text(
                '#!/bin/sh\nexec /snap/bin/chromium "$@"\n',
                encoding="utf-8",
            )
            snap.chmod(0o755)
            native = Path(tmp) / "chromium-browser"
            native.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            native.chmod(0o755)
            paths = {
                "google-chrome-stable": None,
                "google-chrome": None,
                "chromium": str(snap),
                "chromium-browser": str(native),
            }

            with patch.object(
                scraper.shutil, "which", side_effect=paths.get
            ):
                self.assertEqual(
                    scraper._find_system_chromium(), str(native)
                )

    def test_launch_chromium_accepts_explicit_native_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            native = Path(tmp) / "chrome"
            native.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            native.chmod(0o755)
            browser_type = Mock()
            browser_type.executable_path = "/browser/managed/absent"
            playwright = Mock(chromium=browser_type)

            with patch.dict(
                scraper.os.environ,
                {"CHROMIUM_EXECUTABLE_PATH": str(native)},
            ):
                scraper._launch_chromium(playwright)

            browser_type.launch.assert_called_once_with(
                headless=True, executable_path=str(native)
            )

    def test_launch_chromium_explains_how_to_install_missing_browser(self):
        browser_type = Mock()
        browser_type.executable_path = "/browser/managed/absent"
        playwright = Mock(chromium=browser_type)

        with patch.object(scraper, "_CHROMIUM_PATH", ""), \
                patch.object(scraper, "_find_system_chromium", return_value=""), \
                patch.dict(
                    scraper.os.environ,
                    {"CHROMIUM_EXECUTABLE_PATH": ""},
                ):
            with self.assertRaisesRegex(
                scraper.PlaywrightBrowserUnavailable,
                r"playwright>=1\.61,<2",
            ):
                scraper._launch_chromium(playwright)
        browser_type.launch.assert_not_called()

    def test_launch_chromium_rejects_explicit_snap_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "chromium-browser"
            wrapper.write_text(
                '#!/bin/sh\nexec /snap/bin/chromium "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            browser_type = Mock()
            browser_type.executable_path = "/browser/managed/absent"
            playwright = Mock(chromium=browser_type)

            with patch.dict(
                scraper.os.environ,
                {"CHROMIUM_EXECUTABLE_PATH": str(wrapper)},
            ):
                with self.assertRaisesRegex(
                    scraper.PlaywrightBrowserUnavailable,
                    r"Snap",
                ):
                    scraper._launch_chromium(playwright)
        browser_type.launch.assert_not_called()

    def test_playwright_failure_is_short_and_not_repeated_for_same_site(self):
        error = scraper.PlaywrightBrowserUnavailable(
            "Chromium absent\nBrowser logs:\n" + ("bruit " * 1000)
        )
        scraper._PLAYWRIGHT_FAILURES_REPORTED.clear()
        try:
            with patch.object(scraper, "log") as log:
                scraper._log_playwright_failure(
                    "https://www.myscience.ch/fr/jobs/Education", error
                )
                scraper._log_playwright_failure(
                    "https://www.myscience.ch/fr/jobs/Media", error
                )
        finally:
            scraper._PLAYWRIGHT_FAILURES_REPORTED.clear()

        log.assert_called_once()
        message = log.call_args.args[0]
        self.assertTrue(message.startswith("Erreur Playwright"))
        self.assertIn("Chromium absent", message)
        self.assertIn("source ignorée", message)
        self.assertNotIn("Browser logs", message)

    def test_fetch_without_playwright_is_reported_as_disabled(self):
        scraper._PLAYWRIGHT_FAILURES_REPORTED.clear()
        try:
            with patch.object(scraper, "PLAYWRIGHT_AVAILABLE", False), \
                    patch.object(scraper, "log") as log:
                self.assertIsNone(
                    scraper.fetch_via_playwright(
                        "https://www.myscience.ch/fr/jobs/Education"
                    )
                )
        finally:
            scraper._PLAYWRIGHT_FAILURES_REPORTED.clear()

        log.assert_called_once()
        self.assertIn("source ignorée", log.call_args.args[0])
        self.assertIn("non installée", log.call_args.args[0])

    def test_job_room_propagates_missing_browser(self):
        unavailable = scraper.PlaywrightBrowserUnavailable("Chromium absent")
        playwright_context = MagicMock()

        with patch.object(scraper, "ACTIVE_PROFILE", "systemes"), \
                patch.object(scraper, "PLAYWRIGHT_AVAILABLE", True), \
                patch.object(scraper, "robots_allows", return_value=True), \
                patch.object(
                    scraper, "_sync_playwright",
                    return_value=playwright_context,
                ), \
                patch.object(
                    scraper, "_launch_chromium", side_effect=unavailable
                ):
            with self.assertRaises(scraper.PlaywrightBrowserUnavailable):
                scraper.scrape_job_room()

    @patch.object(scraper.time, "sleep")
    @patch.object(scraper, "_polite_wait")
    @patch.object(scraper, "session")
    def test_adzuna_retries_a_503_without_leaking_or_failing(
        self, session, _wait, sleep
    ):
        unavailable = Mock(status_code=503, headers={})
        success = Mock(status_code=200, headers={})
        session.return_value.get.side_effect = [unavailable, success]

        response = scraper._adzuna_get(
            "https://api.adzuna.test/jobs?app_key=secret", retries=2
        )

        self.assertIs(response, success)
        self.assertEqual(session.return_value.get.call_count, 2)
        sleep.assert_called_once()

    def test_reliefweb_without_approved_appname_is_disabled_once(self):
        previous = scraper.RELIEFWEB_APPNAME
        scraper.RELIEFWEB_APPNAME = ""
        try:
            with patch.object(scraper, "_reliefweb_items") as api, \
                    patch.object(scraper, "log") as log:
                self.assertEqual(scraper.scrape_reliefweb(), [])
        finally:
            scraper.RELIEFWEB_APPNAME = previous
        api.assert_not_called()
        log.assert_called_once()
        self.assertIn("pré-approuvé absent", log.call_args.args[0])

    def test_atomic_json_recovers_previous_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            scraper._atomic_write_json(path, {"version": 1})
            scraper._atomic_write_json(path, {"version": 2})
            path.write_text("{fichier tronqué", encoding="utf-8")
            self.assertEqual(
                scraper._load_json_file(path, {}), {"version": 1}
            )

    def test_health_distinguishes_filtered_empty_from_error(self):
        health = {}
        alerts = scraper.update_health(
            "source_test", 0, health, raw=12, source_field="test.example",
            status="filtered", duration_ms=1250,
        )
        self.assertEqual(alerts, [])
        self.assertEqual(health["source_test"]["last_status"], "filtered")
        self.assertEqual(health["source_test"]["duration_ms"], 1250)
        self.assertIn("last_success_at", health["source_test"])

        alerts = scraper.update_health(
            "source_test", 0, health, raw=0, source_field="test.example",
            status="error", duration_ms=20, error="HTTP 500",
        )
        self.assertEqual(health["source_test"]["last_status"], "error")
        self.assertEqual(health["source_test"]["consecutive_failures"], 1)
        self.assertIn("HTTP 500", health["source_test"]["last_error"])
        self.assertEqual(len(alerts), 1)

        alerts = scraper.update_health(
            "source_optionnelle", 0, health, raw=0,
            source_field="option.example", status="disabled",
            duration_ms=5, error="configuration absente",
        )
        self.assertEqual(alerts, [])
        self.assertEqual(
            health["source_optionnelle"]["last_status"], "disabled"
        )

    def test_detail_cache_survives_a_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "detail_cache.json"
            previous_path = scraper.DETAIL_CACHE_FILE
            previous_cache = scraper._detail_fields_cache
            try:
                scraper.DETAIL_CACHE_FILE = cache_path
                scraper._detail_fields_cache = {}
                scraper._cache_detail_fields(
                    "https://example.test/job/42?utm_source=test",
                    {"description": "Description suffisamment détaillée.",
                     "location": "Genève"},
                )
                scraper._save_detail_cache()
                scraper._detail_fields_cache = {}
                scraper._load_detail_cache()
                cached = scraper._cached_detail_fields(
                    "https://example.test/job/42"
                )
            finally:
                scraper.DETAIL_CACHE_FILE = previous_path
                scraper._detail_fields_cache = previous_cache
            self.assertEqual(cached["location"], "Genève")

    def test_detail_candidates_are_enriched_in_stable_priority_order(self):
        scraper.configure_profile("lettres")
        previous_defer = scraper._DEFER_DETAIL_FETCHES
        previous_fetch = scraper.FETCH_DESCRIPTIONS
        previous_local = scraper.FETCH_LOCAL_DETAILS
        jobs = []
        seen = set()
        calls = []

        def details(_url, title):
            calls.append(title)
            return {
                "description": (
                    "Gestion documentaire, records management et "
                    "knowledge management."
                ),
                "location": "Genève",
                "company": "Organisation test",
            }

        try:
            scraper._DEFER_DETAIL_FETCHES = True
            scraper.FETCH_DESCRIPTIONS = True
            scraper.FETCH_LOCAL_DETAILS = True
            scraper._pending_detail_candidates.clear()
            with patch.object(scraper, "fetch_detail_fields", side_effect=details):
                scraper.consider(
                    "Communications Manager", "https://example.test/job/3",
                    {"company": "Organisation test", "source": "test",
                     "location": "Genève"},
                    jobs, seen,
                )
                scraper.consider(
                    "Bibliothécaire documentaliste", "https://example.test/job/2",
                    {"company": "Organisation test", "source": "test",
                     "location": ""},
                    jobs, seen,
                )
                scraper.consider(
                    "Coordinateur de programme", "https://example.test/job/1",
                    {"company": "Organisation test", "source": "test",
                     "location": "Genève"},
                    jobs, seen,
                )
                scraper._DEFER_DETAIL_FETCHES = False
                with patch.object(scraper, "log"):
                    scraper._process_pending_detail_candidates()
        finally:
            scraper._pending_detail_candidates.clear()
            scraper._DEFER_DETAIL_FETCHES = previous_defer
            scraper.FETCH_DESCRIPTIONS = previous_fetch
            scraper.FETCH_LOCAL_DETAILS = previous_local

        self.assertEqual(calls, [
            "Coordinateur de programme",
            "Bibliothécaire documentaliste",
            "Communications Manager",
        ])
        self.assertEqual(len(jobs), 3)

    def test_detail_order_is_fair_between_sources(self):
        scraper.configure_profile("lettres")

        def candidate(source, suffix):
            return {
                "title": "Communications Manager",
                "url": f"https://example.test/job/{suffix}",
                "description": "",
                "fields": {"source": source, "company": "Organisation test",
                           "location": "Genève"},
                "trusted_geo": False,
                "geo_ok": True,
            }

        pending = [
            candidate("source-a", "a3"),
            candidate("source-a", "a1"),
            candidate("source-b", "b2"),
            candidate("source-a", "a2"),
            candidate("source-b", "b1"),
        ]
        order = scraper._fair_detail_order(pending)
        self.assertEqual(
            [item["fields"]["source"] for item in order],
            ["source-a", "source-b", "source-a", "source-b", "source-a"],
        )


class IdentityTests(unittest.TestCase):
    def test_canonical_url_removes_tracking_but_keeps_job_fragment(self):
        self.assertEqual(
            scraper.canonical_url(
                "HTTPS://Example.test/job/42/?utm_source=mail&b=2&a=1#top"
            ),
            "https://example.test/job/42?a=1&b=2",
        )
        self.assertEqual(
            scraper.canonical_url(
                "https://offres-emploi.vd.ch/#fr/job/REQ-123"
            ),
            "https://offres-emploi.vd.ch/#fr/job/REQ-123",
        )

    def test_external_id_and_title_metadata_are_deduplicated(self):
        first = {
            "title": "Responsable éditorial F/H (100%)",
            "company": "Organisation test",
            "location": "Genève",
            "source": "ATS",
            "url": "https://ats.example.test/job/ancienne-url",
            "external_id": "REQ-42",
        }
        second = {
            **first,
            "title": "Responsable éditorial",
            "url": "https://ats.example.test/job/nouvelle-url",
        }
        self.assertEqual(len(scraper.deduplicate_jobs([first, second])), 1)
        self.assertEqual(scraper.tracking_id(first), scraper.tracking_id(second))

        distinct_requisition = {
            **second,
            "external_id": "REQ-43",
        }
        self.assertEqual(
            len(scraper.deduplicate_jobs([first, distinct_requisition])), 2
        )

    def test_multilingual_variants_of_the_same_offer_are_deduplicated(self):
        base = {
            "company": "Academic Work Switzerland SA",
            "location": "Genève",
            "source": "test",
        }
        jobs = [
            {
                **base,
                "title": "Consultant Kubernetes dans le domaine bancaire",
                "url": "https://example.test/fr/42",
            },
            {
                **base,
                "title": "Kubernetes Berater im Bankwesen",
                "url": "https://example.test/de/42",
            },
            {
                **base,
                "title": "Consulente Kubernetes nel settore bancario",
                "url": "https://example.test/it/42",
            },
        ]
        self.assertEqual(len(scraper.deduplicate_jobs(jobs)), 1)

    def test_legal_company_suffixes_do_not_prevent_deduplication(self):
        jobs = [
            {
                "title": "Senior Financial Controller",
                "company": "Deloitte AG",
                "url": "https://example.test/job/1",
            },
            {
                "title": "Senior Financial Controller",
                "company": "Deloitte",
                "url": "https://example.test/job/2",
            },
        ]
        self.assertEqual(len(scraper.deduplicate_jobs(jobs)), 1)


if __name__ == "__main__":
    unittest.main()
