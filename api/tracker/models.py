from django.db import models


class Company(models.Model):
    name = models.CharField(max_length=200, unique=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contact(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.company.name})"


class Application(models.Model):
    class Status(models.TextChoices):
        SAVED = "saved", "Saved"
        # Materials generated and the record queued, but nothing submitted yet.
        # This is the "pending" state the Google Sheet shows.
        READY = "ready", "Ready to submit"
        # She's read the draft and okayed it, but hasn't submitted yet. Separate
        # from READY so "generated for me" and "I've actually approved this" are
        # never confused — only the second is safe to act on.
        APPROVED = "approved", "Approved to send"
        APPLIED = "applied", "Applied"
        PHONE_SCREEN = "phone_screen", "Phone screen"
        INTERVIEW = "interview", "Interview"
        OFFER = "offer", "Offer"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"
        # Removed from the pipeline before it was ever sent. Distinct from
        # WITHDRAWN, which means she pulled out of a live application, and not a
        # deletion — the generated text cost money and the PDFs still exist.
        DISCARDED = "discarded", "Removed"

    class Source(models.TextChoices):
        MANUAL = "manual", "Added manually"
        INGESTED = "ingested", "From ingestion pipeline"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="applications")
    # The posting this came from, so the sheet can show its score and generated
    # materials and the auto-filler can find the apply URL. String reference
    # keeps tracker from importing ingestion, which imports tracker.
    # SET_NULL: losing a scraped posting must not delete a real application.
    source_posting = models.ForeignKey(
        "ingestion.IngestedPosting",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications",
    )
    role_title = models.CharField(max_length=200)
    job_url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SAVED)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    applied_date = models.DateField(null=True, blank=True)
    salary_notes = models.CharField(max_length=200, blank=True)
    resume = models.FileField(upload_to="applications/resumes/", blank=True, null=True)
    cover_letter = models.FileField(upload_to="applications/cover_letters/", blank=True, null=True)
    # Drive copies of the two PDFs above — what she actually opens when filling
    # in an employer's form, and what the Google Sheet links to.
    resume_drive_url = models.URLField(max_length=500, blank=True)
    cover_letter_drive_url = models.URLField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.role_title} @ {self.company.name}"


class ApplicationEvent(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    note = models.CharField(max_length=500)
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.application} — {self.note[:40]}"
