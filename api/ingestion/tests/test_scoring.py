from django.test import SimpleTestCase

from ingestion.scoring import score_posting


def posting(**overrides):
    base = {
        "title": "Coordinator",
        "companyName": "Some Company",
        "descriptionText": "",
        "location": {"city": "Louisville", "formattedAddressShort": "Louisville, KY"},
        "isRemote": False,
        "age": "10 days ago",
    }
    base.update(overrides)
    return base


class ScoringTests(SimpleTestCase):
    def test_a_real_match_outranks_the_noise_indeed_returns(self):
        # The actual failure mode: a "director of development" search returning
        # nursing roles. The real one has to win by a wide margin.
        good, _ = score_posting(posting(
            title="Director of Development",
            companyName="Louisville Urban League",
            descriptionText="Lead fundraising, grant writing, and donor stewardship for our nonprofit.",
        ))
        noise, _ = score_posting(posting(title="RN/LPN", companyName="Sansbury Care Center"))
        self.assertGreater(good, noise + 40)

    def test_each_lane_is_recognized(self):
        for title in ["Director of Development", "Grant Writer", "Communications Manager",
                      "Digital Content Specialist", "Program Director"]:
            score, reasons = score_posting(posting(title=title))
            self.assertTrue(
                any("lane" in reason for reason in reasons),
                f"{title!r} should match a lane, got {reasons}",
            )

    def test_clinical_and_trades_roles_are_pushed_down(self):
        for title in ["Registered Nurse", "CDL Truck Driver", "Construction Supervisor",
                      "Pharmacy Technician", "Line Cook"]:
            score, reasons = score_posting(posting(title=title))
            self.assertLess(score, 35, f"{title!r} scored {score}")
            self.assertTrue(any("noise" in reason for reason in reasons))

    def test_remote_and_local_both_score_well_but_distant_onsite_does_not(self):
        remote, _ = score_posting(posting(title="Grant Writer", isRemote=True, location={"city": ""}))
        local, _ = score_posting(posting(title="Grant Writer"))
        distant, _ = score_posting(posting(
            title="Grant Writer",
            location={"city": "Phoenix", "formattedAddressShort": "Phoenix, AZ"},
        ))
        self.assertGreater(remote, distant)
        self.assertGreater(local, distant)

    def test_hourly_roles_are_penalized_without_inventing_a_target_salary(self):
        hourly, reasons = score_posting(posting(
            title="Program Coordinator",
            salary={"salaryType": "hourly", "salaryText": "From $20 an hour"},
        ))
        salaried, _ = score_posting(posting(
            title="Program Coordinator",
            salary={"salaryType": "yearly", "salaryMax": 75000},
        ))
        self.assertGreater(salaried, hourly)
        self.assertIn("Hourly wage role", reasons)

    def test_junior_titles_rank_below_the_same_role_at_her_level(self):
        senior, _ = score_posting(posting(title="Development Director"))
        junior, _ = score_posting(posting(title="Development Assistant"))
        self.assertGreater(senior, junior)

    def test_nonprofit_employers_get_credit(self):
        with_signal, reasons = score_posting(posting(
            title="Communications Manager",
            descriptionText="Our nonprofit foundation serves youth across the community.",
        ))
        without, _ = score_posting(posting(title="Communications Manager"))
        self.assertGreater(with_signal, without)
        self.assertTrue(any("Nonprofit" in reason for reason in reasons))

    def test_fresh_postings_edge_out_stale_ones(self):
        fresh, _ = score_posting(posting(title="Grant Writer", age="1 day ago"))
        stale, _ = score_posting(posting(title="Grant Writer", age="21 days ago"))
        self.assertGreater(fresh, stale)

    def test_score_always_lands_in_range_and_reasons_are_readable(self):
        for item in [posting(), posting(title="RN"), posting(title="Director of Development"), {}]:
            score, reasons = score_posting(item)
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)
            self.assertIsInstance(reasons, list)


class UnpaidRoleTests(SimpleTestCase):
    """
    "Volunteer Grant Writer" scored a perfect 100 in production: it matches the
    grants lane on every keyword. She needs paid work, so it can't sit at the
    top of the list.
    """

    def test_volunteer_roles_score_zero_however_well_they_match(self):
        score, reasons = score_posting(posting(
            title="Volunteer Grant Writer",
            companyName="Some Foundation",
            descriptionText="Write grants, manage funder compliance, and steward donors.",
        ))
        self.assertEqual(score, 0)
        self.assertIn("Unpaid or volunteer position", reasons)

    def test_internships_and_unpaid_titles_are_caught_too(self):
        for title in ("Marketing Intern ", "Unpaid Development Associate", "Pro Bono Grant Writer"):
            with self.subTest(title=title):
                self.assertEqual(score_posting(posting(title=title))[0], 0)

    def test_managing_volunteers_is_still_a_real_job(self):
        # The word appears constantly in perfectly good paid postings — this is
        # why the check is on the title only, never the description.
        score, reasons = score_posting(posting(
            title="Director of Development",
            companyName="Louisville Urban League",
            descriptionText="Recruit and manage volunteers, run our internship program, write grants.",
        ))
        self.assertGreater(score, 0)
        self.assertNotIn("Unpaid or volunteer position", reasons)


class ATSDetectionTests(SimpleTestCase):
    """
    Which portals demand an account before showing the form. Getting this wrong
    in the "requires account" direction is worse than being unsure: it would put
    a warning badge on a perfectly open application.
    """

    def test_account_gated_platforms_are_flagged_with_a_sign_in_url(self):
        from ingestion.ats import describe

        workday = describe("https://healogics.wd5.myworkdayjobs.com/healogics/job/x/Program-Director_JR1")
        self.assertEqual(workday["platform"], "Workday")
        self.assertTrue(workday["requires_account"])
        # A Workday tenant's bare origin 406s; the site name carries the login.
        self.assertEqual(workday["sign_in_url"],
                         "https://healogics.wd5.myworkdayjobs.com/healogics/login")

        # Some tenants put a locale before the site name; dropping it gives
        # /en-US/login, which 404s.
        localised = describe(
            "https://uofl.wd1.myworkdayjobs.com/en-US/UofLCareerSite/job/HSC/Program-Manager_R1")
        self.assertEqual(localised["sign_in_url"],
                         "https://uofl.wd1.myworkdayjobs.com/en-US/UofLCareerSite/login")

        icims = describe("https://external-canoncareers.icims.com/jobs/34822/job")
        self.assertTrue(icims["requires_account"])
        self.assertEqual(icims["sign_in_url"], "https://external-canoncareers.icims.com/")

    def test_open_platforms_are_not_flagged(self):
        from ingestion.ats import describe

        for url in ("https://jobs.smartrecruiters.com/Dungarvin/744000146516192",
                    "https://pdga.bamboohr.com/careers/37",
                    "https://boards.greenhouse.io/acme/jobs/1"):
            with self.subTest(url=url):
                result = describe(url)
                self.assertFalse(result["requires_account"])
                self.assertEqual(result["sign_in_url"], "")

    def test_indeed_is_not_treated_as_account_gated(self):
        # Half of Indeed's links bounce to the employer's own site. Flagging
        # them all badged 25 of 59 postings and pointed at indeed.com's
        # homepage, which told her nothing.
        from ingestion.ats import describe

        result = describe("http://www.indeed.com/job/director-development-fabad")
        self.assertEqual(result["platform"], "Indeed")
        self.assertFalse(result["requires_account"])
        self.assertEqual(result["sign_in_url"], "")

    def test_unknown_hosts_are_assumed_open(self):
        from ingestion.ats import describe

        for url in ("https://heart.jobs/clarksburg-wv/lead/22C6", "", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(describe(url)["requires_account"])
