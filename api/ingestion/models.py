from django.db import models


class IngestedPosting(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        TRIAGED = "triaged", "Triaged"
        DISMISSED = "dismissed", "Dismissed"
        # The employer took the listing down. Kept rather than deleted: an
        # application may point at it, and its materials cost real money.
        EXPIRED = "expired", "No longer listed"

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
            # alone rather than (source, url): the same posting is the same job
            # no matter which search happened to surface it. Partial, because
            # blank URLs would otherwise all collide with each other.
            models.UniqueConstraint(
                fields=["url"],
                condition=~models.Q(url=""),
                name="unique_posting_per_url",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.source})"
