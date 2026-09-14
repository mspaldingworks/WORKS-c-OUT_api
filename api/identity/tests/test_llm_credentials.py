from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from identity import llm
from identity.encryption import decrypt, encrypt
from identity.models import LLMCredential


class EncryptionTests(TestCase):
    def test_round_trip(self):
        secret = "sk-test-abcdEFGH1234"
        self.assertEqual(decrypt(encrypt(secret)), secret)

    def test_ciphertext_hides_plaintext(self):
        self.assertNotIn("sk-test", encrypt("sk-test-xyz"))


class LLMCredentialModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")

    def test_key_stored_encrypted_and_masked(self):
        cred = LLMCredential(owner=self.user, provider="openai")
        cred.set_key("sk-secret-ABCD1234")
        cred.save()
        self.assertNotIn("secret", cred.api_key_encrypted)
        self.assertEqual(cred.get_key(), "sk-secret-ABCD1234")
        self.assertEqual(cred.masked_key, "…1234")


class ResolveConfigTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")

    def test_none_without_active(self):
        self.assertIsNone(llm.resolve_config(self.user))

    def test_returns_active_with_provider_defaults(self):
        cred = LLMCredential(owner=self.user, provider="openai", is_active=True)
        cred.set_key("sk-abc")
        cred.save()
        config = llm.resolve_config(self.user)
        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.api_key, "sk-abc")
        self.assertEqual(config.model, "gpt-4o")                       # from PROVIDERS
        self.assertEqual(config.base_url, "https://api.openai.com/v1")  # from PROVIDERS


class CompleteDispatchTests(TestCase):
    @patch("identity.llm._complete_openai", return_value="OPENAI")
    @patch("identity.llm._complete_anthropic", return_value="ANTHROPIC")
    def test_none_config_uses_anthropic(self, mock_anthropic, mock_openai):
        with self.settings(ANTHROPIC_API_KEY="server-key"):
            self.assertEqual(llm.complete("sys", "user", config=None), "ANTHROPIC")
        mock_openai.assert_not_called()

    @patch("identity.llm._complete_openai", return_value="OPENAI")
    @patch("identity.llm._complete_anthropic", return_value="ANTHROPIC")
    def test_openai_config_uses_openai(self, mock_anthropic, mock_openai):
        config = llm.LLMConfig(provider="openai", api_key="k", model="gpt-4o",
                               base_url="https://api.openai.com/v1")
        self.assertEqual(llm.complete("sys", "user", config=config), "OPENAI")
        mock_anthropic.assert_not_called()

    def test_no_key_anywhere_raises(self):
        with self.settings(ANTHROPIC_API_KEY=""):
            with self.assertRaises(llm.LLMUnavailable):
                llm.complete("sys", "user", config=None)


class LLMCredentialEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tester", password="x")
        self.token = Token.objects.create(user=self.user)
        self.list_url = reverse("llmcredential-list")

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Token {self.token.key}"}

    def test_create_masks_key_and_never_returns_it(self):
        resp = self.client.post(
            self.list_url,
            data={"provider": "openai", "api_key": "sk-secret-9876"},
            content_type="application/json", **self._auth(),
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertNotIn("api_key", body)
        self.assertNotIn("secret", str(body))
        self.assertEqual(body["masked_key"], "…9876")
        self.assertTrue(body["is_active"])  # save & use

    def test_list_returns_masked_only(self):
        cred = LLMCredential(owner=self.user, provider="deepseek")
        cred.set_key("sk-deep-4321")
        cred.save()
        body = self.client.get(self.list_url, **self._auth()).json()
        self.assertEqual(body[0]["masked_key"], "…4321")
        self.assertNotIn("sk-deep-4321", str(body))
        self.assertNotIn("api_key_encrypted", str(body))

    def test_activate_switches_active(self):
        a = LLMCredential(owner=self.user, provider="openai", is_active=True)
        a.set_key("k1"); a.save()
        b = LLMCredential(owner=self.user, provider="deepseek")
        b.set_key("k2"); b.save()
        resp = self.client.post(reverse("llmcredential-activate", args=[b.pk]), **self._auth())
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertFalse(a.is_active)
        self.assertTrue(b.is_active)

    def test_custom_requires_base_url_and_model(self):
        resp = self.client.post(
            self.list_url,
            data={"provider": "custom", "api_key": "k"},
            content_type="application/json", **self._auth(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_scoped_per_owner(self):
        other = get_user_model().objects.create_user("other", password="x")
        c = LLMCredential(owner=other, provider="openai")
        c.set_key("k"); c.save()
        self.assertEqual(self.client.get(self.list_url, **self._auth()).json(), [])

    def test_delete_returns_body_and_removes_row(self):
        c = LLMCredential(owner=self.user, provider="openai")
        c.set_key("k1234"); c.save()
        resp = self.client.delete(reverse("llmcredential-detail", args=[c.pk]), **self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(LLMCredential.objects.filter(pk=c.pk).count(), 0)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.list_url).status_code, 401)
