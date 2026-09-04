"""
WORKS(c)OUT moved from one implicit profile to real per-account ownership.
These guard the actual point of that change: one account's rows are
invisible and unreachable to another account, not just filtered out of a
default list view.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity.models import ProfessionalProfile, ProfileLink, Skill


def auth_header(user):
    return {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


class SkillOwnershipTests(TestCase):
    def setUp(self):
        self.alice = get_user_model().objects.create_user("alice", password="x")
        self.bob = get_user_model().objects.create_user("bob", password="x")
        self.alice_auth = auth_header(self.alice)
        self.bob_auth = auth_header(self.bob)
        self.alice_skill = Skill.objects.create(owner=self.alice, name="Python")

    def test_a_skill_list_only_shows_the_caller_s_own_rows(self):
        Skill.objects.create(owner=self.bob, name="Fundraising")
        response = self.client.get(reverse("skill-list"), **self.alice_auth)
        names = [row["name"] for row in response.json()]
        self.assertEqual(names, ["Python"])

    def test_another_account_s_skill_is_a_404_not_someone_else_s_data(self):
        response = self.client.get(
            reverse("skill-detail", args=[self.alice_skill.pk]), **self.bob_auth
        )
        self.assertEqual(response.status_code, 404)

    def test_another_account_cannot_edit_or_delete_it_either(self):
        detail = reverse("skill-detail", args=[self.alice_skill.pk])
        self.assertEqual(
            self.client.patch(detail, {"proficiency": "expert"},
                              content_type="application/json", **self.bob_auth).status_code,
            404,
        )
        self.assertEqual(self.client.delete(detail, **self.bob_auth).status_code, 404)
        self.alice_skill.refresh_from_db()
        self.assertEqual(self.alice_skill.proficiency, Skill.Proficiency.COMPETENT)

    def test_two_accounts_can_each_have_a_skill_with_the_same_name(self):
        # Uniqueness moved from global to (owner, name) specifically so this
        # would work — otherwise the second account's first "Python" 400s.
        response = self.client.post(
            reverse("skill-list"), {"name": "Python"},
            content_type="application/json", **self.bob_auth,
        )
        self.assertEqual(response.status_code, 201)

    def test_creating_a_skill_attaches_it_to_the_caller_automatically(self):
        response = self.client.post(
            reverse("skill-list"), {"name": "Grant Writing", "category": "Fundraising"},
            content_type="application/json", **self.bob_auth,
        )
        self.assertEqual(response.status_code, 201)
        created = Skill.objects.get(pk=response.json()["id"])
        self.assertEqual(created.owner, self.bob)


class ProfessionalProfileOwnershipTests(TestCase):
    def setUp(self):
        self.alice = get_user_model().objects.create_user("alice", password="x")
        self.bob = get_user_model().objects.create_user("bob", password="x")

    def test_each_account_gets_its_own_profile(self):
        ProfessionalProfile.objects.create(owner=self.alice, headline="Director")
        response = self.client.get(reverse("professionalprofile-list"), **auth_header(self.bob))
        self.assertEqual(response.json(), [])

    def test_a_second_profile_for_the_same_account_is_a_clear_error_not_a_500(self):
        auth = auth_header(self.alice)
        first = self.client.post(reverse("professionalprofile-list"), {"headline": "Director"},
                                 content_type="application/json", **auth)
        self.assertEqual(first.status_code, 201)

        second = self.client.post(reverse("professionalprofile-list"), {"headline": "Again"},
                                  content_type="application/json", **auth)
        self.assertEqual(second.status_code, 400)


class ProfileLinkAndResumeOwnershipTests(TestCase):
    def setUp(self):
        self.alice = get_user_model().objects.create_user("alice", password="x")
        self.bob = get_user_model().objects.create_user("bob", password="x")

    def test_profile_links_are_scoped_per_account(self):
        ProfileLink.objects.create(owner=self.alice, platform="LinkedIn", url="https://linkedin.test/alice")
        response = self.client.get(reverse("profilelink-list"), **auth_header(self.bob))
        self.assertEqual(response.json(), [])

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(reverse("skill-list")).status_code, 401)
        self.assertEqual(self.client.get(reverse("resumeversion-list")).status_code, 401)
