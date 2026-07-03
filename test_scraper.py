import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch

import scraper


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
        response.json.return_value = {
            "total": 1,
            "jobPostings": [{
                "title": "Ingénieur Système Linux",
                "externalPath": "/job/Geneva/Linux_R001",
                "locationsText": "Genève, Suisse",
            }],
        }
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

    @patch.object(scraper, "_polite_wait")
    @patch.object(scraper, "session")
    def test_smartrecruiters_adapter_keeps_only_local_relevant_job(
        self, session, _wait
    ):
        response = Mock()
        response.json.return_value = {
            "totalFound": 2,
            "content": [
                {"id": "1", "name": "System Administrator Linux",
                 "ref": "https://api.smartrecruiters.com/v1/companies/Test/postings/1",
                 "location": {"city": "Geneva", "country": "Switzerland"}},
                {"id": "2", "name": "System Administrator Linux",
                 "ref": "https://api.smartrecruiters.com/v1/companies/Test/postings/2",
                 "location": {"city": "Zurich", "country": "Switzerland"}},
            ],
        }
        session.return_value.get.return_value = response
        jobs = []

        scraper._scrape_smartrecruiters_source({
            "name": "Employeur Test", "source": "ATS test",
            "company_id": "Test",
        }, jobs, set())

        self.assertEqual([job["url"] for job in jobs], [
            "https://jobs.smartrecruiters.com/Test/1-system-administrator-linux"
        ])


if __name__ == "__main__":
    unittest.main()
