from django.db import models


class ProfessionalProfile(models.Model):
    """Single-row bio/summary. Intended usage: exactly one instance via the admin."""

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

    name = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=100, blank=True)
    proficiency = models.CharField(max_length=20, choices=Proficiency.choices, default=Proficiency.COMPETENT)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name


class ProfileLink(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_UPDATE = "needs_update", "Needs update"
        STALE = "stale", "Stale"

    platform = models.CharField(max_length=100)
    url = models.URLField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["platform"]

    def __str__(self):
        return f"{self.platform} ({self.get_status_display()})"


class ResumeVersion(models.Model):
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to="identity/resumes/")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
