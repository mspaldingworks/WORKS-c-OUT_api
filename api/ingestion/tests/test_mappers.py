import json
from pathlib import Path

from django.test import SimpleTestCase

from ingestion.mappers import fetch_dataset_items, normalize_item

# Captured verbatim from a real borderline/indeed-scraper run (only the bulky
# description fields were shortened). Guessed fixtures are worth little here —
# this is the actual shape the pipeline has to survive.
REAL_INDEED_ITEM = json.loads((Path(__file__).parent / "fixture_indeed_real.json").read_text())

# Representative item shapes from the four board Actors. These are the fragile
# part of the pipeline — Actors change their output and this is what catches it.
INDEED_ITEM = {
    "positionName": "Senior Program Manager",
    "company": "Acme Nonprofit",
    "jobUrl": "https://www.indeed.com/viewjob?jk=abc123",
    "location": "Louisville, KY",
    "salary": "$70,000 - $85,000 a year",
}

LINKEDIN_ITEM = {
    "title": "Director of Operations",
    "companyName": "Globex",
    "link": "https://www.linkedin.com/jobs/view/1234567890",
    "postedAt": "2026-08-30",
}

GLASSDOOR_ITEM = {
    "jobTitle": "Development Coordinator",
    "employer": {"name": "Initech Foundation"},
    "applyUrl": "https://www.glassdoor.com/job-listing/JV_KO0,20.htm",
}

ZIPRECRUITER_ITEM = {
    "name": "Grants Manager",
    "organization": "Hooli Community Fund",
    "url": "https://www.ziprecruiter.com/c/Hooli/Job/Grants-Manager",
}


class RealIndeedOutputTests(SimpleTestCase):
    """Against genuine Actor output, not a hand-written approximation."""

    def test_maps_a_real_indeed_item(self):
        mapped = normalize_item(REAL_INDEED_ITEM, "apify:indeed")
        self.assertEqual(mapped["title"], "Innovation Program Manager")
        self.assertEqual(mapped["company_name"], "Sazerac")
        self.assertEqual(mapped["source"], "apify:indeed")

    def test_prefers_the_canonical_listing_url_over_the_ats_apply_link(self):
        # Real items carry both. jobUrl is the stable Indeed listing; applyUrl
        # points at whatever external ATS the employer uses.
        mapped = normalize_item(REAL_INDEED_ITEM, "apify:indeed")
        self.assertEqual(mapped["url"], REAL_INDEED_ITEM["jobUrl"])
        self.assertNotEqual(mapped["url"], REAL_INDEED_ITEM["applyUrl"])

    def test_items_own_source_field_does_not_overwrite_ours(self):
        # Indeed items contain a "source" key of their own (the employer name),
        # which must not leak into the model's source column.
        self.assertEqual(REAL_INDEED_ITEM["source"], "Sazerac Company")
        mapped = normalize_item(REAL_INDEED_ITEM, "apify:indeed")
        self.assertEqual(mapped["source"], "apify:indeed")

    def test_structured_fields_survive_in_raw_payload(self):
        # location and salary come back as dicts, not strings — they have no
        # column, so they must be preserved intact for later use.
        mapped = normalize_item(REAL_INDEED_ITEM, "apify:indeed")
        self.assertEqual(mapped["raw_payload"]["location"]["city"], "Louisville")
        self.assertEqual(mapped["raw_payload"]["salary"]["salaryMax"], 190000)


class NormalizeItemTests(SimpleTestCase):
    def test_maps_indeed_shape(self):
        mapped = normalize_item(INDEED_ITEM, "apify:indeed")
        self.assertEqual(mapped["title"], "Senior Program Manager")
        self.assertEqual(mapped["company_name"], "Acme Nonprofit")
        self.assertEqual(mapped["url"], "https://www.indeed.com/viewjob?jk=abc123")
        self.assertEqual(mapped["source"], "apify:indeed")

    def test_maps_linkedin_shape(self):
        mapped = normalize_item(LINKEDIN_ITEM, "apify:linkedin")
        self.assertEqual(mapped["title"], "Director of Operations")
        self.assertEqual(mapped["company_name"], "Globex")
        self.assertEqual(mapped["url"], "https://www.linkedin.com/jobs/view/1234567890")

    def test_maps_glassdoor_shape_with_nested_employer(self):
        mapped = normalize_item(GLASSDOOR_ITEM, "apify:glassdoor")
        self.assertEqual(mapped["title"], "Development Coordinator")
        self.assertEqual(mapped["company_name"], "Initech Foundation")

    def test_maps_ziprecruiter_shape(self):
        mapped = normalize_item(ZIPRECRUITER_ITEM, "apify:ziprecruiter")
        self.assertEqual(mapped["title"], "Grants Manager")
        self.assertEqual(mapped["company_name"], "Hooli Community Fund")

    def test_keeps_everything_else_in_raw_payload(self):
        mapped = normalize_item(INDEED_ITEM, "apify:indeed")
        # Fields with no column (location, salary) must survive for later use.
        self.assertEqual(mapped["raw_payload"]["location"], "Louisville, KY")
        self.assertEqual(mapped["raw_payload"]["salary"], "$70,000 - $85,000 a year")

    def test_skips_items_with_no_usable_title(self):
        self.assertIsNone(normalize_item({"company": "Acme", "url": "https://x.test"}, "apify:indeed"))
        self.assertIsNone(normalize_item({"title": "   "}, "apify:indeed"))
        self.assertIsNone(normalize_item("not a dict", "apify:indeed"))

    def test_tolerates_missing_company_and_url(self):
        mapped = normalize_item({"title": "Analyst"}, "apify:indeed")
        self.assertEqual(mapped["company_name"], "")
        self.assertEqual(mapped["url"], "")

    def test_truncates_overlong_title_and_company(self):
        mapped = normalize_item({"title": "T" * 400, "company": "C" * 300}, "apify:indeed")
        self.assertEqual(len(mapped["title"]), 300)
        self.assertEqual(len(mapped["company_name"]), 200)

    def test_drops_url_too_long_for_the_column(self):
        # Truncating a URL makes it useless, so it's dropped rather than stored broken.
        mapped = normalize_item({"title": "Analyst", "url": "https://x.test/" + "a" * 1200}, "apify:indeed")
        self.assertEqual(mapped["url"], "")
        self.assertIn("a" * 1200, mapped["raw_payload"]["url"])


class FetchDatasetItemsTests(SimpleTestCase):
    def test_rejects_dataset_ids_that_arent_plain_alphanumeric(self):
        # The id comes from an external webhook body and is interpolated into an
        # outbound URL, so anything unexpected must be refused before the call.
        for bad_id in ["../../evil", "abc/def", "abc?token=x", "", None, "abc def"]:
            with self.assertRaises(ValueError):
                fetch_dataset_items(bad_id)
