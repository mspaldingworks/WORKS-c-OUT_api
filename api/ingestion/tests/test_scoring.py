from django.test import SimpleTestCase

from ingestion.scoring import BLANK_PROFILE, ScoringProfile, score_posting

LOUISVILLE = (38.2527, -85.7585)
CHICAGO = (41.8781, -87.6298)
JEFFERSONVILLE = (38.2776, -85.7372)


def posting(**overrides):
    base = {
        "title": "Coordinator",
        "companyName": "Some Company",
        "descriptionText": "",
        "isRemote": False,
        "age": "10 days ago",
    }
    base.update(overrides)
    return base


class BlankAccountTests(SimpleTestCase):
    """An account with nothing set must not be ranked against anyone else's
    field, lanes or city."""

    def test_no_skills_means_no_skill_points_and_a_nudge(self):
        fit = score_posting(posting(title="Director of Development",
                                    descriptionText="Fundraising and grant writing."))
        self.assertEqual(fit.matched_skills, [])
        self.assertIn("Add a résumé in Identity to rank jobs by your skills", fit.reasons)
        self.assertFalse(any("lane" in reason for reason in fit.reasons))

    def test_nursing_and_development_score_alike_with_no_profile(self):
        nursing = score_posting(posting(title="RN/LPN")).score
        development = score_posting(posting(title="Director of Development")).score
        self.assertEqual(nursing, development)

    def test_no_city_is_assumed(self):
        fit = score_posting(posting(), BLANK_PROFILE, latitude=LOUISVILLE[0], longitude=LOUISVILLE[1])
        self.assertFalse(any("Louisville" in reason or "mi from home" in reason for reason in fit.reasons))


class SkillTests(SimpleTestCase):
    profile = ScoringProfile(skills=("Grant Writing", "Fundraising", "Python"))

    def test_a_skill_match_outranks_an_unrelated_job(self):
        good = score_posting(posting(
            title="Grant Writer",
            descriptionText="Grant writing and fundraising for our programs.",
        ), self.profile)
        noise = score_posting(posting(title="RN/LPN", descriptionText="Patient care."), self.profile)
        self.assertGreater(good.score, noise.score + 30)
        self.assertEqual(good.matched_skills, ["Fundraising", "Grant Writing"])

    def test_title_match_is_reported(self):
        fit = score_posting(posting(title="Grant Writer"), self.profile)
        self.assertTrue(any(reason.startswith("Title matches your Grant Writing") for reason in fit.reasons))

    def test_matching_is_word_bounded(self):
        # "Python" must not be found inside "Pythonic" — substring matching
        # inflates fit and makes the skill filter lie.
        fit = score_posting(posting(descriptionText="A Pythonic mindset."), self.profile)
        self.assertNotIn("Python", fit.matched_skills)

    def test_aliases_count_as_the_skill(self):
        fit = score_posting(posting(descriptionText="Experience with annual fund campaigns."), self.profile)
        self.assertIn("Fundraising", fit.matched_skills)


class LocationTests(SimpleTestCase):
    profile = ScoringProfile(home=LOUISVILLE, radius_miles=25)

    def test_nearby_beats_far_away(self):
        near = score_posting(posting(), self.profile, latitude=JEFFERSONVILLE[0], longitude=JEFFERSONVILLE[1])
        far = score_posting(posting(), self.profile, latitude=CHICAGO[0], longitude=CHICAGO[1])
        self.assertGreater(near.score, far.score)
        self.assertTrue(any("mi from home" in reason for reason in near.reasons))

    def test_remote_never_loses_points_for_distance(self):
        remote = score_posting(posting(), self.profile, is_remote=True,
                               latitude=CHICAGO[0], longitude=CHICAGO[1])
        far = score_posting(posting(), self.profile, is_remote=False,
                            latitude=CHICAGO[0], longitude=CHICAGO[1])
        self.assertGreater(remote.score, far.score)
        self.assertIn("Remote", remote.reasons)

    def test_unknown_location_is_neutral(self):
        fit = score_posting(posting(), self.profile)
        self.assertFalse(any("mi from home" in reason for reason in fit.reasons))


class RuleTests(SimpleTestCase):
    def test_unpaid_work_scores_zero_however_well_it_matches(self):
        profile = ScoringProfile(skills=("Grant Writing",))
        fit = score_posting(posting(title="Volunteer Grant Writer",
                                    descriptionText="Grant writing for our gala."), profile)
        self.assertEqual(fit.score, 0)
        self.assertIn("Unpaid or volunteer position", fit.reasons)

    def test_fresh_postings_get_a_bump(self):
        fresh = score_posting(posting(age="Just posted")).score
        stale = score_posting(posting(age="30+ days ago")).score
        self.assertGreater(fresh, stale)

    def test_score_is_clamped(self):
        profile = ScoringProfile(skills=tuple(f"Skill{i}" for i in range(20)), home=LOUISVILLE)
        text = " ".join(f"Skill{i}" for i in range(20))
        fit = score_posting(posting(title=text, descriptionText=text, age="Just posted"), profile,
                            latitude=LOUISVILLE[0], longitude=LOUISVILLE[1])
        self.assertLessEqual(fit.score, 100)

    def test_not_a_dict_scores_zero(self):
        self.assertEqual(score_posting(None).score, 0)


class SignatureTests(SimpleTestCase):
    def test_signature_changes_with_skills_home_and_radius(self):
        base = ScoringProfile(skills=("A",), home=LOUISVILLE, radius_miles=25)
        self.assertNotEqual(base.signature(), ScoringProfile(skills=("A", "B"), home=LOUISVILLE).signature())
        self.assertNotEqual(base.signature(), ScoringProfile(skills=("A",), home=CHICAGO).signature())
        self.assertNotEqual(base.signature(), ScoringProfile(skills=("A",), home=LOUISVILLE,
                                                             radius_miles=50).signature())

    def test_skill_order_does_not_matter(self):
        self.assertEqual(ScoringProfile(skills=("A", "B")).signature(),
                         ScoringProfile(skills=("B", "A")).signature())
