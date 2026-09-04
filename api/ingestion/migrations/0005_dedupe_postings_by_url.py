from django.db import migrations


def collapse_duplicate_urls(apps, schema_editor):
    """
    Merge postings that share a URL, keeping the one worth keeping.

    Dedupe used to be on (source, url), so the same job found by two different
    searches was stored twice — and promoted twice, producing two applications
    for one job. This has to run before the new unique-on-url constraint, which
    would otherwise fail against the existing rows.
    """
    IngestedPosting = apps.get_model("ingestion", "IngestedPosting")
    Application = apps.get_model("tracker", "Application")

    seen = {}
    for posting in IngestedPosting.objects.exclude(url="").order_by("id"):
        seen.setdefault(posting.url, []).append(posting)

    for url, postings in seen.items():
        if len(postings) < 2:
            continue

        # Prefer a posting that already has generated materials (they cost real
        # money), then one that's been triaged, then the oldest.
        keeper = sorted(
            postings,
            key=lambda p: (bool(p.generated_materials), p.status == "triaged", -p.id),
            reverse=True,
        )[0]

        for posting in postings:
            if posting.pk == keeper.pk:
                continue
            # Re-point rather than orphan: source_posting is SET_NULL, so a
            # plain delete would strip the application of its score, materials
            # and apply URL.
            Application.objects.filter(source_posting=posting).update(source_posting=keeper)
            posting.delete()

    # Collapse the duplicate applications those duplicate postings produced.
    for posting in IngestedPosting.objects.all():
        applications = list(
            Application.objects.filter(source_posting=posting).order_by("id")
        )
        if len(applications) < 2:
            continue
        # Keep whichever records the most progress; ties go to the oldest.
        applications.sort(key=lambda a: (a.applied_date is not None, a.status != "saved", -a.id), reverse=True)
        for duplicate in applications[1:]:
            duplicate.delete()


def noop(apps, schema_editor):
    """Deleted rows can't be recreated; the constraint swap below is reversible."""


class Migration(migrations.Migration):

    dependencies = [
        ("ingestion", "0004_ingestedposting_generated_materials"),
        ("tracker", "0003_application_cover_letter_drive_url_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="ingestedposting",
            name="unique_posting_per_source_url",
        ),
        migrations.RunPython(collapse_duplicate_urls, noop),
    ]
