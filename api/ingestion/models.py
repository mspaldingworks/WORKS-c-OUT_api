from django.conf import settings
from django.db import models


class IngestedPosting(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        TRIAGED = "triaged", "Triaged"
        DISMISSED = "dismissed", "Dismissed"
        # The employer took the listing down. Kept rather than deleted: an
        # application may point at it, and its materials cost real money.
        EXPIRED = "expired", "No longer listed"

    # Ingestion has no authenticated request (it's a webhook), so this is set
    # from identity.owners.get_default_owner() rather than request.user.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ingested_postings")
    source = models.CharField(max_length=100, help_text="e.g. apify:indeed, rss:indeed, email")
    title = models.CharField(max_length=300)
    company_name = models.CharField(max_length=200, blank=True)
    # Job-board URLs routinely blow past URLField's 200-char default once
    # tracking/query params are attached, so this is widened deliberately.
    url = models.URLField(max_length=1000, blank=True)
    # Where the application actually lives — usually the employer's ATS, which
    # is a different destination from the job-board listing in `url`.
    apply_url = models.URLField(max_length=1000, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    # Fit against her profile, 0-100, computed at ingest. Stored rather than
    # computed on read so the ordering is cheap and the reasoning is auditable.
    score = models.PositiveSmallIntegerField(default=0, db_index=True)
    score_reasons = models.JSONField(default=list, blank=True)
    # Filterable facets denormalized from raw_payload at ingest, so the feed can
    # be narrowed in the database rather than by unpacking every payload on read.
    # Salary is annualized (hourly rates × 2080, etc.) so one range compares
    # across pay periods; null means the posting listed no pay. See salary.py.
    salary_min_annual = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    salary_max_annual = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    is_remote = models.BooleanField(default=False, db_index=True)
    # Normalized lowercase tokens ("full_time", "contract") for __contains
    # filtering without reaching back into raw_payload.
    employment_types = models.JSONField(default=list, blank=True)
    # Cached cover letter / tailored resume, so re-opening a posting doesn't
    # re-run (and re-pay for) generation.
    generated_materials = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Best fit first; recency breaks ties.
        ordering = ["-score", "-created_at"]
        constraints = [
            # A recurring scrape re-sees the same jobs every run, and the six
            # per-lane searches overlap heavily — "Program Manager" comes back
            # from both the programs and development queries. Dedupe on url
            # per owner rather than globally: the same posting is the same job
            # no matter which search happened to surface it, but two different
            # accounts seeing the same listing must not collide with each
            # other. Partial, because blank URLs would otherwise all collide.
            models.UniqueConstraint(
                fields=["owner", "url"],
                condition=~models.Q(url=""),
                name="unique_posting_per_url_per_owner",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.source})"
