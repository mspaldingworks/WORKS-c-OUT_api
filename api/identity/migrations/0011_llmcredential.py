import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('identity', '0010_jobfilterpreferences'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LLMCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(choices=[('anthropic', 'Claude (Anthropic)'), ('openai', 'OpenAI'), ('gemini', 'Google Gemini'), ('deepseek', 'DeepSeek'), ('custom', 'Custom (OpenAI-compatible)')], max_length=20)),
                ('api_key_encrypted', models.TextField()),
                ('model', models.CharField(blank=True, max_length=100)),
                ('base_url', models.URLField(blank=True)),
                ('is_active', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='llm_credentials', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['provider'],
            },
        ),
        migrations.AddConstraint(
            model_name='llmcredential',
            constraint=models.UniqueConstraint(fields=('owner', 'provider'), name='unique_llm_provider_per_owner'),
        ),
    ]
