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
