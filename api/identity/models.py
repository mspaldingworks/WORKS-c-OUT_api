from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from .validators import validate_resume_file


class ProfessionalProfile(models.Model):
    """Bio/summary — one row per account."""

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="professional_profile")

    headline = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    # Canonical career history in plain text — the source material the
    # application-materials generator tailors from. Kept as one field rather
    # than a normalized schema because it's fed to a model as prose, and
    # over-structuring it would lose the phrasing she actually uses.
    master_resume = models.TextField(blank=True)

    # Contact details, kept here rather than in the master resume text because
    # the auto-filler needs them as discrete fields to match against a form.
    # legal_name is separate from anything display-oriented: it's what goes on
    # an application, which is not always how someone is introduced.
    legal_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    city_state = models.CharField(max_length=120, blank=True)
    # Portals routinely mark street address and ZIP required, so autofill can't
    # finish a form without them. Kept separate from city_state, which is what
    # goes on the resume header.
    street_address = models.CharField(max_length=200, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    linkedin_url = models.URLField(blank=True)
    portfolio_url = models.URLField(blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @property
    def first_name(self):
        """Portals almost always split the name; store it once, split on read."""
        return self.legal_name.split(" ", 1)[0] if self.legal_name else ""

    @property
    def last_name(self):
        parts = self.legal_name.split(" ", 1)
        return parts[1] if len(parts) > 1 else ""

    class Meta:
        verbose_name = "professional profile"

    def __str__(self):
        return self.headline or "Professional profile"


class Skill(models.Model):
    class Proficiency(models.TextChoices):
        LEARNING = "learning", "Learning"
        COMPETENT = "competent", "Competent"
        STRONG = "strong", "Strong"
        EXPERT = "expert", "Expert"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="skills")
    # Not globally unique — two different accounts can each have "Python".
    # Uniqueness is per owner instead (see Meta.constraints).
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=100, blank=True)
    proficiency = models.CharField(max_length=20, choices=Proficiency.choices, default=Proficiency.COMPETENT)

    class Meta:
        ordering = ["category", "name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="unique_skill_name_per_owner"),
        ]

    def __str__(self):
        return self.name


class ProfileLink(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_UPDATE = "needs_update", "Needs update"
        STALE = "stale", "Stale"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile_links")
    platform = models.CharField(max_length=100)
    url = models.URLField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["platform"]

    def __str__(self):
        return f"{self.platform} ({self.get_status_display()})"


class ResumeVersion(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="resumes")
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to="identity/resumes/", validators=[validate_resume_file])
    notes = models.TextField(blank=True)
    # AI-extracted skill/profile suggestions from this file (see
    # resume_parsing.py). Staged here for review — nothing here is ever
    # written into Skill/ProfessionalProfile automatically; the Identity tab
    # applies whichever suggestions she wants through the ordinary endpoints.
    parsed_data = models.JSONField(default=dict, blank=True)
    # Removing a résumé is reversible, like discarding an application: the app's
    # own rules ask for undo rather than a confirmation dialog, and undo can only
    # work if the row (and the parsed suggestions it cost a model call to
    # produce) is still here. Null means active.
    discarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class JobFilterPreferences(models.Model):
    """Which optional Job-Feed filters this account wants surfaced.

    The feed can offer several filters (salary, remote, job type, match score),
    but showing all of them at once is noise — the point is to let each account
    pick the few it cares about so the filter bar stays legible. One row per
    account, created with defaults on first read (see the API view). Salary is on
    by default because it's the filter the feature was built around.
    """

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="job_filter_preferences")
    salary = models.BooleanField(default=True)
    remote = models.BooleanField(default=False)
    job_type = models.BooleanField(default=False)
    match_score = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "job filter preferences"
        verbose_name_plural = "job filter preferences"

    def __str__(self):
        return f"Job filter preferences for {self.owner}"


class LLMCredential(models.Model):
    """One account's own API key for an AI provider, so their generation and
    résumé parsing run on their subscription rather than the server's.

    The key is stored encrypted (see encryption.py) and never returned to the
    client — only a masked hint. One row per provider per account; the single
    `is_active` row is the one actually used, falling back to the server's
    Anthropic key when there's no active row (see llm.resolve_config).
    """

    class Provider(models.TextChoices):
        ANTHROPIC = "anthropic", "Claude (Anthropic)"
        OPENAI = "openai", "OpenAI"
        GEMINI = "gemini", "Google Gemini"
        DEEPSEEK = "deepseek", "DeepSeek"
        # Any OpenAI-compatible endpoint (OpenRouter, Groq, local, …): the user
        # supplies base_url + model themselves.
        CUSTOM = "custom", "Custom (OpenAI-compatible)"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="llm_credentials")
    provider = models.CharField(max_length=20, choices=Provider.choices)
    api_key_encrypted = models.TextField()
    # Optional overrides; blank means "use the provider default" (see llm.PROVIDERS).
    model = models.CharField(max_length=100, blank=True)
    base_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "provider"], name="unique_llm_provider_per_owner"),
        ]

    def set_key(self, raw):
        from .encryption import encrypt

        self.api_key_encrypted = encrypt(raw)

    def get_key(self):
        from .encryption import decrypt

        return decrypt(self.api_key_encrypted)

    @property
    def masked_key(self):
        """A hint only — never the key. Shows the last 4 chars, e.g. '…4a9f'."""
        try:
            raw = self.get_key()
        except Exception:
            return ""
        return f"…{raw[-4:]}" if len(raw) > 4 else "…"

    def __str__(self):
        return f"{self.get_provider_display()} key for {self.owner}"


class GoogleDriveConnection(models.Model):
    """One account's own Google Drive, so their generated cover letters and
    résumés are mirrored to *their* Drive instead of the server owner's.

    The refresh token is stored encrypted (same Fernet helper as LLMCredential)
    and never returned to the client. `enabled` is the Identity toggle; uploads
    only happen when there's a token AND enabled is true (see
    tracker.drive.resolve_drive). One row per account.
    """

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name="google_drive_connection")
    refresh_token_encrypted = models.TextField(blank=True)
    # The app-created Drive folder the PDFs go into, and the connected account,
    # for display. drive.file scope means the app only ever sees files it made.
    folder_id = models.CharField(max_length=200, blank=True)
    account_email = models.CharField(max_length=254, blank=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_token(self, raw):
        from .encryption import encrypt

        self.refresh_token_encrypted = encrypt(raw)

    def get_token(self):
        from .encryption import decrypt

        return decrypt(self.refresh_token_encrypted) if self.refresh_token_encrypted else ""

    @property
    def connected(self):
        return bool(self.refresh_token_encrypted)

    def __str__(self):
        return f"Drive connection for {self.owner}"


class MagicLinkToken(models.Model):
    """A one-time sign-in link for an account with no usable password.

    Accounts provisioned for a partner app's members (see `partner_views`) are
    reached through that app's token. This is what makes them reachable
    without it: the member asks for a link, clicks it, and gets a session
    here — so the account is genuinely theirs rather than only nominally.

    Only a hash of the token is stored, the same reasoning as a password: a
    database copy must not hand someone a working sign-in link. The plaintext
    exists once, in the email.
    """

    LIFETIME = timedelta(minutes=15)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="magic_links",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Magic link for {self.user}"

    @property
    def is_usable(self):
        return self.used_at is None and timezone.now() < self.expires_at
