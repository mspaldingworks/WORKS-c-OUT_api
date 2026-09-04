from django.test import SimpleTestCase

from ingestion.details import describe


class Posting:
    def __init__(self, payload):
        self.raw_payload = payload


class DetailsTests(SimpleTestCase):
    def test_pulls_the_readable_fields_from_a_real_payload_shape(self):
        details = describe(Posting({
            "descriptionText": "Lead our digital fundraising.",
            "location": {"city": "Carlsbad", "formattedAddressShort": "Carlsbad, CA",
                         "latitude": 33.16},
            "salary": {"salaryMin": 70000, "salaryMax": 80000,
                       "salaryText": "$70,000 - $80,000 a year", "salaryType": "yearly"},
            "jobType": ["Full-time", "Remote"],
            "benefits": ["Health insurance", "Paid holidays"],
            "age": "3 hours ago",
            "isRemote": True,
            "rating": {"rating": 2.8, "count": 10},
        }))

        self.assertEqual(details["location"], "Carlsbad, CA")
        self.assertEqual(details["salary"], "$70,000 - $80,000 a year")
        self.assertEqual(details["job_types"], ["Full-time", "Remote"])
        self.assertEqual(details["benefits"], ["Health insurance", "Paid holidays"])
        self.assertEqual(details["posted"], "3 hours ago")
        self.assertTrue(details["is_remote"])
        self.assertEqual(details["company_rating"], "2.8 from 10 reviews")
        self.assertIn("digital fundraising", details["description"])

    def test_builds_a_salary_range_when_there_is_no_prose_version(self):
        details = describe(Posting({"salary": {"salaryMin": 60000, "salaryMax": 75000}}))
        self.assertEqual(details["salary"], "$60,000 – $75,000")

    def test_survives_the_shapes_scrapers_actually_return(self):
        # Every one of these has turned up: a bare string location, salary as a
        # non-dict, a single job type rather than a list, and nothing at all.
        for payload in ({}, {"location": "Louisville, KY"}, {"salary": "negotiable"},
                        {"jobType": "Full-time"}, {"rating": {"count": 4}},
                        {"benefits": None}, {"location": {"city": "Louisville"}}):
            with self.subTest(payload=payload):
                details = describe(Posting(payload))
                self.assertIsInstance(details["job_types"], list)
                self.assertIsInstance(details["benefits"], list)
                self.assertIsInstance(details["description"], str)
                self.assertIsInstance(details["is_remote"], bool)

    def test_a_single_job_type_string_becomes_a_list(self):
        self.assertEqual(describe(Posting({"jobType": "Full-time"}))["job_types"], ["Full-time"])

    def test_a_rating_without_a_count_still_reads(self):
        self.assertEqual(describe(Posting({"rating": {"rating": 3.5}}))["company_rating"], "3.5")

    def test_a_bare_string_location_is_kept(self):
        self.assertEqual(describe(Posting({"location": "Louisville, KY"}))["location"],
                         "Louisville, KY")
