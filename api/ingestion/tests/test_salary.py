from django.test import SimpleTestCase

from ingestion.mappers import derive_facets, normalize_item
from ingestion.salary import HOURS_PER_YEAR, parse_salary


class ParseSalaryTests(SimpleTestCase):
    def test_numeric_yearly_range(self):
        result = parse_salary({"salary": {
            "salaryMin": 80000, "salaryMax": 100000, "salaryText": "$80,000 - $100,000 a year",
        }})
        self.assertEqual(result, {"salary_min_annual": 80000, "salary_max_annual": 100000})

    def test_hourly_is_annualized(self):
        result = parse_salary({"salary": {
            "salaryMin": 25, "salaryMax": 30, "salaryText": "$25 - $30 an hour",
        }})
        self.assertEqual(result, {
            "salary_min_annual": 25 * HOURS_PER_YEAR,
            "salary_max_annual": 30 * HOURS_PER_YEAR,
        })

    def test_hourly_without_period_uses_magnitude_heuristic(self):
        # No period stated and the figures are small — treated as hourly, not a
        # $25/yr job.
        result = parse_salary({"salary": {"salaryMin": 25, "salaryMax": 30}})
        self.assertEqual(result, {"salary_min_annual": 52000, "salary_max_annual": 62400})

    def test_text_only_numbers_are_parsed(self):
        result = parse_salary({"salary": {"salaryText": "$90,000 per year"}})
        self.assertEqual(result, {"salary_min_annual": 90000, "salary_max_annual": 90000})

    def test_no_usable_salary(self):
        self.assertEqual(parse_salary({}), {"salary_min_annual": None, "salary_max_annual": None})
        self.assertEqual(
            parse_salary({"salary": "competitive"}),
            {"salary_min_annual": None, "salary_max_annual": None},
        )
        self.assertEqual(
            parse_salary({"salary": {"salaryText": "Competitive"}}),
            {"salary_min_annual": None, "salary_max_annual": None},
        )

    def test_min_over_max_is_reordered(self):
        result = parse_salary({"salary": {"salaryMin": 100000, "salaryMax": 80000}})
        self.assertEqual(result, {"salary_min_annual": 80000, "salary_max_annual": 100000})


class DeriveFacetsTests(SimpleTestCase):
    def test_remote_and_job_types_normalized(self):
        facets = derive_facets({"isRemote": True, "jobType": ["Full-time", "Contract"]})
        self.assertTrue(facets["is_remote"])
        self.assertEqual(facets["employment_types"], ["full_time", "contract"])

    def test_job_type_string_becomes_list(self):
        self.assertEqual(derive_facets({"jobType": "Part-time"})["employment_types"], ["part_time"])

    def test_non_dict_is_safe(self):
        self.assertEqual(
            derive_facets(None),
            {"salary_min_annual": None, "salary_max_annual": None,
             "is_remote": False, "employment_types": []},
        )

    def test_normalize_item_carries_facets(self):
        mapped = normalize_item(
            {"title": "Nurse", "salary": {"salaryText": "$40 an hour"},
             "isRemote": False, "jobType": "Full-time"},
            source="apify:indeed",
        )
        self.assertEqual(mapped["salary_min_annual"], 40 * HOURS_PER_YEAR)
        self.assertEqual(mapped["employment_types"], ["full_time"])
        self.assertFalse(mapped["is_remote"])
