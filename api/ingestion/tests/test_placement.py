import datetime
import json
from pathlib import Path

from django.test import SimpleTestCase

from ingestion.geo import miles_between
from ingestion.mappers import derive_facets
from ingestion.placement import HYBRID, ONSITE, REMOTE, coordinates, posted_on, work_arrangement

FIXTURE = Path(__file__).with_name("fixture_indeed_real.json")


class WorkArrangementTests(SimpleTestCase):
    def test_real_indeed_posting_is_onsite(self):
        item = json.loads(FIXTURE.read_text())
        item = item[0] if isinstance(item, list) else item
        self.assertEqual(work_arrangement(item), ONSITE)

    def test_is_remote_flag(self):
        self.assertEqual(work_arrangement({"title": "Grants Manager", "isRemote": True}), REMOTE)

    def test_remote_location_string(self):
        self.assertEqual(work_arrangement({"title": "Writer", "location": "Remote"}), REMOTE)

    def test_hybrid_beats_the_remote_flag(self):
        # Indeed marks "Hybrid remote in Louisville, KY" as isRemote — but it's a commute.
        item = {"title": "Program Manager", "isRemote": True,
                "location": {"formattedAddressShort": "Hybrid remote in Louisville, KY"}}
        self.assertEqual(work_arrangement(item), HYBRID)

    def test_hybrid_attribute(self):
        item = {"title": "Coordinator", "attributes": ["Hybrid work", "Health insurance"],
                "location": {"city": "Louisville"}}
        self.assertEqual(work_arrangement(item), HYBRID)

    def test_hybrid_in_description_needs_work_context(self):
        working = {"title": "Analyst", "location": {"city": "Louisville"},
                   "descriptionText": "This is a hybrid role with two office days."}
        cloud = {"title": "Engineer", "location": {"city": "Louisville"},
                 "descriptionText": "Experience with hybrid cloud infrastructure."}
        self.assertEqual(work_arrangement(working), HYBRID)
        self.assertEqual(work_arrangement(cloud), ONSITE)

    def test_hybrid_product_in_title_is_not_a_hybrid_role(self):
        item = {"title": "Hybrid Cloud Architect", "location": {"city": "Louisville"}}
        self.assertEqual(work_arrangement(item), ONSITE)

    def test_no_location_and_no_signal_is_unknown(self):
        self.assertEqual(work_arrangement({"title": "Director"}), "")

    def test_facets_keep_is_remote_in_step(self):
        hybrid = derive_facets({"title": "PM", "isRemote": True, "location": "Hybrid remote in Louisville, KY"})
        self.assertEqual(hybrid["work_arrangement"], HYBRID)
        self.assertFalse(hybrid["is_remote"])


class CoordinateTests(SimpleTestCase):
    def test_reads_latitude_and_longitude(self):
        self.assertEqual(coordinates({"location": {"latitude": 38.25, "longitude": -85.51}}), (38.25, -85.51))

    def test_null_island_and_garbage_are_dropped(self):
        self.assertEqual(coordinates({"location": {"latitude": 0, "longitude": 0}}), (None, None))
        self.assertEqual(coordinates({"location": {"latitude": "x", "longitude": 1}}), (None, None))
        self.assertEqual(coordinates({"location": {"latitude": 95, "longitude": 1}}), (None, None))
        self.assertEqual(coordinates({"location": "Louisville, KY"}), (None, None))

    def test_haversine_sanity(self):
        # Louisville to Lexington is about 70 miles as the crow flies.
        self.assertAlmostEqual(miles_between(38.2527, -85.7585, 38.0406, -84.5037), 70, delta=3)


class PostedOnTests(SimpleTestCase):
    scraped = datetime.date(2026, 9, 1)

    def test_date_published_wins(self):
        item = {"datePublished": "2026-08-07", "age": "1 day ago"}
        self.assertEqual(posted_on(item, self.scraped), datetime.date(2026, 8, 7))

    def test_iso_timestamp(self):
        self.assertEqual(posted_on({"datePublished": "2026-08-07T14:00:00Z"}, self.scraped),
                         datetime.date(2026, 8, 7))

    def test_relative_age_counts_back_from_the_scrape(self):
        self.assertEqual(posted_on({"age": "3 days ago"}, self.scraped), datetime.date(2026, 8, 29))
        self.assertEqual(posted_on({"age": "30+ days ago"}, self.scraped), datetime.date(2026, 8, 2))
        self.assertEqual(posted_on({"age": "Just posted"}, self.scraped), self.scraped)
        self.assertEqual(posted_on({"postedToday": True}, self.scraped), self.scraped)

    def test_unreadable_is_none(self):
        self.assertIsNone(posted_on({"datePublished": "soon"}, self.scraped))
        self.assertIsNone(posted_on({}, self.scraped))
