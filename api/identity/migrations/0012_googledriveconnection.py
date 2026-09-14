import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('identity', '0011_llmcredential'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GoogleDriveConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('refresh_token_encrypted', models.TextField(blank=True)),
                ('folder_id', models.CharField(blank=True, max_length=200)),
                ('account_email', models.CharField(blank=True, max_length=254)),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='google_drive_connection', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
