import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('identity', '0009_magic_link_token'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='JobFilterPreferences',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('salary', models.BooleanField(default=True)),
                ('remote', models.BooleanField(default=False)),
                ('job_type', models.BooleanField(default=False)),
                ('match_score', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='job_filter_preferences', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'job filter preferences',
                'verbose_name_plural': 'job filter preferences',
            },
        ),
    ]
