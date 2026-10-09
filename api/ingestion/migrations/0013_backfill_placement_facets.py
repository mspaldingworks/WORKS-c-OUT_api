from django.db import migrations

FIELDS = ["is_remote", "work_arrangement", "latitude", "longitude", "posted_at"]


def backfill_placement(apps, schema_editor):
    """
    Fill the workplace, coordinates and listing-date columns from each stored
    row's raw_payload, so postings scraped before the distance and posted-within
    filters existed can be filtered and sorted without a re-scrape. Uses the
    same derivation as ingest, counting a relative "3 days ago" back from the
    day each row was scraped. is_remote is recomputed too: a hybrid role is no
    longer counted as remote.
    """
    from ingestion.mappers import derive_facets

    IngestedPosting = apps.get_model("ingestion", "IngestedPosting")
    postings = list(IngestedPosting.objects.all())
    for posting in postings:
        facets = derive_facets(posting.raw_payload or {}, posting.created_at.date())
        for field in FIELDS:
            setattr(posting, field, facets[field])
    IngestedPosting.objects.bulk_update(postings, FIELDS, batch_size=200)


def noop(apps, schema_editor):
    """Reversing 0012 drops the columns; there is nothing to undo here."""


class Migration(migrations.Migration):

    dependencies = [
        ("ingestion", "0012_ingestedposting_placement_facets"),
    ]

    operations = [
        migrations.RunPython(backfill_placement, noop),
    ]
