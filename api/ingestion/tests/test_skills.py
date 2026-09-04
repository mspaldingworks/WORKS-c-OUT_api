from django.test import TestCase

from ingestion.models import IngestedPosting
from ingestion.skills import summarise

HERS = ("Fundraising", "Grant Writing", "WordPress", "Git", "CSS", "Python",
        "Google Analytics", "Copywriting", "Event Planning")


def posting(description, title="Director of Development"):
    return IngestedPosting.objects.create(
        source="apify:indeed", title=title, company_name="Acme",
        url=f"https://example.test/{abs(hash(description)) % 10**8}",
        raw_payload={"descriptionText": description, "title": title})


class SkillMatchingTests(TestCase):
    def test_finds_her_skills_by_name_and_by_how_postings_phrase_them(self):
        result = summarise(posting(
            "You'll lead fundraising and grant writing, and maintain our WordPress site."
        ), HERS)
        self.assertIn("Fundraising", result["matched"])
        self.assertIn("Grant Writing", result["matched"])
        self.assertIn("WordPress", result["matched"])

    def test_word_boundaries_stop_the_obvious_false_positives(self):
        # Substring matching makes "Git" hit "digital" and "CSS" hit "success",
        # which would overstate her fit on almost every posting.
        result = summarise(posting(
            "A digital role. Success in this position requires strong communication."
        ), HERS)
        self.assertNotIn("Git", result["matched"])
        self.assertNotIn("CSS", result["matched"])

    def test_reports_what_the_job_wants_that_she_does_not_list(self):
        result = summarise(posting(
            "Experience with Salesforce Marketing Cloud and Tableau required. "
            "PMP certification preferred."
        ), HERS)
        self.assertIn("Salesforce Marketing Cloud", result["missing"])
        self.assertIn("Tableau", result["missing"])
        self.assertIn("PMP", result["missing"])

    def test_a_skill_she_has_is_never_reported_as_missing(self):
        result = summarise(posting("Must know WordPress and Google Analytics."),
                           HERS + ("Tableau",))
        self.assertNotIn("Tableau", result["missing"])
        self.assertIn("WordPress", result["matched"])

    def test_an_empty_posting_yields_nothing_rather_than_failing(self):
        result = summarise(posting(""), HERS)
        self.assertEqual(result, {"matched": [], "missing": []})

    def test_results_are_sorted_so_the_display_is_stable(self):
        result = summarise(posting(
            "WordPress, fundraising, copywriting, and event planning."), HERS)
        self.assertEqual(result["matched"], sorted(result["matched"]))


class AliasBreadthTests(TestCase):
    """
    The first version matched only near-literal names, so a Communications
    Manager posting scored zero of her skills. These are the phrasings postings
    actually use — broad enough to be useful, still word-boundary matched.
    """

    def test_ordinary_phrasings_credit_the_right_skill(self):
        cases = [
            ("Responsible for writing and editing member communications.", "Copywriting"),
            ("Build a communications roadmap for the brand.", "Campaign Strategy"),
            ("You will lead a team of four coordinators.", "Staff Supervision"),
            ("Manage donor stewardship and reporting.", "Donor Relations"),
            ("Drive community engagement across the region.", "Public Programming"),
        ]
        for description, expected in cases:
            with self.subTest(expected=expected):
                result = summarise(posting(description), HERS + ("Staff Supervision",
                                   "Donor Relations", "Public Programming", "Campaign Strategy"))
                self.assertIn(expected, result["matched"])

    def test_widening_did_not_break_the_boundary_guard(self):
        result = summarise(posting(
            "A digital-first team. Success requires initiative. We use MySQL."), HERS)
        for wrong in ("Git", "CSS"):
            self.assertNotIn(wrong, result["matched"])
