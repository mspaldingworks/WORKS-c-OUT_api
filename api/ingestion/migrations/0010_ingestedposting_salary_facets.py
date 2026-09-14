from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ingestion", "0009_alter_ingestedposting_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestedposting",
            name="salary_min_annual",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="ingestedposting",
            name="salary_max_annual",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="ingestedposting",
            name="is_remote",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="ingestedposting",
            name="employment_types",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
