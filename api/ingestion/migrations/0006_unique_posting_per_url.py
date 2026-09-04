from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Separate from the row cleanup in 0005 on purpose: Postgres refuses to build
    a unique index in the same transaction that just issued foreign-key updates
    ("cannot CREATE INDEX ... because it has pending trigger events").
    """

    dependencies = [
        ("ingestion", "0005_dedupe_postings_by_url"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="ingestedposting",
            constraint=models.UniqueConstraint(
                condition=models.Q(("url", ""), _negated=True),
                fields=("url",),
                name="unique_posting_per_url",
            ),
        ),
    ]
