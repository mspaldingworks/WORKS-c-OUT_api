"""
Every directly signed-in account has its own feed: its own copy of each
posting, ranked against its own skills and home, filterable by its own skills.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import JobFilterPreferences, ProfessionalProfile, ResumeVersion, Skill
from ingestion.fit import refresh_if_stale, seed_feed
from ingestion.models import IngestedPosting

OWNER_EMAIL = "owner@example.test"
KEY = "test-ingestion-key"

LOUISVILLE = (38.2527, -85.7585)
CHICAGO = (41.8781, -87.6298)

ITEMS = [
    {
        "title": "Grant Writer",
        "companyName": "Helping Hands",
        "jobUrl": "https://www.indeed.com/viewjob?jk=grant1",
        "descriptionText": "Grant writing and donor stewardship for a nonprofit.",
        "isRemote": False,
    },
    {
        # Another board's field names: the mapped title must still be scored.
        "positionName": "Python Developer",
        "company": "Initech",
        "jobUrl": "https://www.indeed.com/viewjob?jk=python1",
        "description": "Build services in Django.",
        "isRemote": True,
    },
]


def make_user(email):
    user = get_user_model().objects.create_user(email, email=email, password="pw-123456789")
    return user, {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


def posting_for(owner, title, url, **extra):
    return IngestedPosting.objects.create(
        owner=owner, source="apify:indeed", title=title, url=url,
        raw_payload={"title": title, "descriptionText": extra.pop("description", "")}, **extra,
    )


@override_settings(INGESTION_API_KEY=KEY, WORKS_COUT_OWNER_EMAIL=OWNER_EMAIL)
class FanOutTests(TestCase):
    def setUp(self):
        self.owner, _ = make_user(OWNER_EMAIL)
        self.other, _ = make_user("someone@example.test")
        self.partner = get_user_model().objects.create_user("partner-transwell-" + "a" * 32)
        Skill.objects.create(owner=self.other, name="Python")

    def _webhook(self):
        payload = {"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"defaultDatasetId": "d1"}}
        with patch("ingestion.views.fetch_dataset_items", return_value=ITEMS):
            return self.client.post(
                f"{reverse('apify-webhook')}?source=indeed&key={KEY}", data=payload,
                content_type="application/json",
            )

    def test_each_direct_account_gets_its_own_copy(self):
        response = self._webhook()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IngestedPosting.objects.filter(owner=self.owner).count(), 2)
        self.assertEqual(IngestedPosting.objects.filter(owner=self.other).count(), 2)

    def test_partner_accounts_are_left_out(self):
        self._webhook()
        self.assertFalse(IngestedPosting.objects.filter(owner=self.partner).exists())

    def test_each_copy_is_scored_against_its_own_owner(self):
        self._webhook()
        mine = IngestedPosting.objects.get(owner=self.other, title="Python Developer")
        theirs = IngestedPosting.objects.get(owner=self.owner, title="Python Developer")
        self.assertEqual(mine.matched_skills, ["Python"])
        self.assertEqual(theirs.matched_skills, [])
        self.assertGreater(mine.score, theirs.score)

    def test_a_rerun_duplicates_nothing(self):
        self._webhook()
        second = self._webhook()
        self.assertEqual(second.json()["created"], 0)
        self.assertEqual(IngestedPosting.objects.filter(owner__in=[self.owner, self.other]).count(), 4)


@override_settings(WORKS_COUT_OWNER_EMAIL=OWNER_EMAIL)
class SeedingTests(TestCase):
    def setUp(self):
        self.owner, _ = make_user(OWNER_EMAIL)
        self.newcomer, self.auth = make_user("new@example.test")
        posting_for(self.owner, "Grant Writer", "https://jobs.test/1",
                    generated_materials={"cover_letter": "Dear…"}, score=90,
                    score_reasons=["Owner's reason"])
        posting_for(self.owner, "Dismissed one", "https://jobs.test/2",
                    status=IngestedPosting.Status.DISMISSED)

    def test_first_feed_load_copies_the_current_feed(self):
        response = self.client.get(reverse("ingestedposting-list"), **self.auth)
        self.assertEqual(response.status_code, 200)
        titles = [row["title"] for row in response.json()]
        self.assertEqual(titles, ["Grant Writer"])

    def test_nothing_personal_is_copied(self):
        seed_feed(self.newcomer)
        copy = IngestedPosting.objects.get(owner=self.newcomer)
        self.assertEqual(copy.generated_materials, {})
        self.assertEqual(copy.status, IngestedPosting.Status.NEW)
        self.assertNotIn("Owner's reason", copy.score_reasons)

    def test_an_account_with_postings_is_not_reseeded(self):
        seed_feed(self.newcomer)
        IngestedPosting.objects.filter(owner=self.newcomer).update(status=IngestedPosting.Status.DISMISSED)
        self.assertEqual(seed_feed(self.newcomer), 0)

    def test_the_owner_is_never_seeded_from_itself(self):
        self.assertEqual(seed_feed(self.owner), 0)


@override_settings(WORKS_COUT_OWNER_EMAIL=OWNER_EMAIL)
class FeedFilterTests(TestCase):
    def setUp(self):
        self.owner, self.auth = make_user(OWNER_EMAIL)
        self.grant = posting_for(self.owner, "Grant Writer", "https://jobs.test/g",
                                 description="Grant writing for our programs.",
                                 latitude=LOUISVILLE[0], longitude=LOUISVILLE[1], work_arrangement="onsite")
        self.python_remote = posting_for(self.owner, "Python Developer", "https://jobs.test/p",
                                         description="Python services.", is_remote=True,
                                         work_arrangement="remote",
                                         latitude=CHICAGO[0], longitude=CHICAGO[1])
        self.far = posting_for(self.owner, "Office Manager", "https://jobs.test/f",
                               latitude=CHICAGO[0], longitude=CHICAGO[1], work_arrangement="onsite")

    def _titles(self, query=""):
        response = self.client.get(reverse("ingestedposting-list") + query, **self.auth)
        self.assertEqual(response.status_code, 200)
        return sorted(row["title"] for row in response.json())

    def test_adding_a_skill_rescores_on_the_next_load(self):
        self.assertEqual(self._titles("?skill=Python"), [])
        Skill.objects.create(owner=self.owner, name="Python")
        self.assertEqual(self._titles("?skill=Python"), ["Python Developer"])
        self.python_remote.refresh_from_db()
        self.assertIn("Python", self.python_remote.matched_skills)

    def test_any_selected_skill_matches(self):
        Skill.objects.create(owner=self.owner, name="Python")
        Skill.objects.create(owner=self.owner, name="Grant Writing")
        self.assertEqual(self._titles("?skill=Python&skill=Grant%20Writing"),
                         ["Grant Writer", "Python Developer"])

    def test_remote_jobs_stay_in_a_radius_whatever_the_distance(self):
        JobFilterPreferences.objects.create(owner=self.owner, home_latitude=LOUISVILLE[0],
                                            home_longitude=LOUISVILLE[1])
        self.assertEqual(self._titles("?within_miles=25"), ["Grant Writer", "Python Developer"])

    def test_remote_can_be_switched_off(self):
        JobFilterPreferences.objects.create(owner=self.owner, home_latitude=LOUISVILLE[0],
                                            home_longitude=LOUISVILLE[1])
        self.assertEqual(self._titles("?exclude_remote=1"), ["Grant Writer", "Office Manager"])
        self.assertEqual(self._titles("?exclude_remote=1&within_miles=25"), ["Grant Writer"])

    def test_moving_home_rescores(self):
        refresh_if_stale(self.owner)
        before = IngestedPosting.objects.get(pk=self.far.pk).score
        JobFilterPreferences.objects.filter(owner=self.owner).update(
            home_latitude=CHICAGO[0], home_longitude=CHICAGO[1])
        self._titles()
        self.assertGreater(IngestedPosting.objects.get(pk=self.far.pk).score, before)

    def test_restoring_a_posting_ranks_it_against_today_s_profile(self):
        self.grant.status = IngestedPosting.Status.DISMISSED
        self.grant.save()
        Skill.objects.create(owner=self.owner, name="Grant Writing")
        response = self.client.post(reverse("ingestedposting-restore", args=[self.grant.pk]), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.grant.refresh_from_db()
        self.assertIn("Grant Writing", self.grant.matched_skills)


class BulkSkillTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user("bulk@example.test")
        Skill.objects.create(owner=self.user, name="Python")

    def test_adds_new_skills_and_skips_ones_already_held(self):
        response = self.client.post(
            reverse("skill-bulk"),
            data={"skills": [
                {"name": "python", "category": "Software", "proficiency": "strong"},
                {"name": "Grant Writing", "category": "Writing", "proficiency": "expert"},
                {"name": "Grant Writing", "category": "Writing", "proficiency": "expert"},
                {"name": "", "category": ""},
            ]},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual([row["name"] for row in response.json()], ["Grant Writing"])
        self.assertEqual(Skill.objects.filter(owner=self.user).count(), 2)

    def test_removing_a_skill_returns_it_for_undo(self):
        skill = Skill.objects.get(owner=self.user, name="Python")
        response = self.client.delete(reverse("skill-detail", args=[skill.pk]), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Python")
        self.assertFalse(Skill.objects.filter(pk=skill.pk).exists())

    def test_an_empty_list_is_refused(self):
        response = self.client.post(reverse("skill-bulk"), data={"skills": []},
                                    content_type="application/json", **self.auth)
        self.assertEqual(response.status_code, 400)


PARSED = {"skills": [], "headline": "", "email": "", "phone": "", "linkedin_url": "", "unparsed": False}


@override_settings(ANTHROPIC_API_KEY="test-key")
class MasterResumeTests(TestCase):
    def setUp(self):
        self.user, self.auth = make_user("resume@example.test")
        with patch("identity.validators.validate_resume_file"):
            self.resume = ResumeVersion.objects.create(
                owner=self.user, title="CV", file=SimpleUploadedFile("cv.pdf", b"%PDF-1.4"),
            )

    def _parse(self):
        with patch("identity.views.extract_resume_text", return_value="Ten years of grant writing."), \
             patch("identity.views.parse_resume_text", return_value=PARSED):
            return self.client.post(reverse("resumeversion-parse", args=[self.resume.pk]), **self.auth)

    def test_the_first_processed_resume_becomes_the_career_history(self):
        self.assertEqual(self._parse().status_code, 200)
        self.assertEqual(ProfessionalProfile.objects.get(owner=self.user).master_resume,
                         "Ten years of grant writing.")

    def test_an_existing_career_history_is_never_overwritten(self):
        ProfessionalProfile.objects.create(owner=self.user, master_resume="Written by hand.")
        self._parse()
        self.assertEqual(ProfessionalProfile.objects.get(owner=self.user).master_resume, "Written by hand.")
