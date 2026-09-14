from django.db import migrations


def backfill_facets(apps, schema_editor):
    """
    Populate the new salary/remote/job-type columns from each row's stored
    raw_payload, so postings scraped before this feature are filterable without
    a re-scrape. Reuses the same derivation ingest uses (ingestion.mappers) so
    old and new rows agree.
    """
    from ingestion.mappers import derive_facets

    IngestedPosting = apps.get_model("ingestion", "IngestedPosting")
    postings = list(IngestedPosting.objects.all())
    for posting in postings:
        facets = derive_facets(posting.raw_payload or {})
        posting.salary_min_annual = facets["salary_min_annual"]
        posting.salary_max_annual = facets["salary_max_annual"]
        posting.is_remote = facets["is_remote"]
        posting.employment_types = facets["employment_types"]
    IngestedPosting.objects.bulk_update(
        postings,
        ["salary_min_annual", "salary_max_annual", "is_remote", "employment_types"],
        batch_size=200,
    )


def noop(apps, schema_editor):
    """Reversing 0010 drops the columns; there is nothing to undo here."""


class Migration(migrations.Migration):

    dependencies = [
        ("ingestion", "0010_ingestedposting_salary_facets"),
    ]

    operations = [
        migrations.RunPython(backfill_facets, noop),
    ]
