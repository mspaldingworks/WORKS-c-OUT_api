import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# nginx terminates TLS and forwards plain HTTP with this header (see deploy/nginx-works-c-out-api.conf).
# Without it, Django thinks every request is HTTP and CSRF's same-origin check on the Origin
# header mismatches against an https:// domain in CSRF_TRUSTED_ORIGINS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "tracker",
    "identity",
    "ingestion",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "works_c_out"),
        "USER": os.environ.get("POSTGRES_USER", "works_c_out"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "works_c_out"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

# Cloud SQL reaches the container over a unix socket, not TCP. Setting
# CLOUD_SQL_CONNECTION_NAME switches to it; everything else stays the same,
# which is what makes this deployable to both Cloud Run and a plain VPS.
if connection_name := os.environ.get("CLOUD_SQL_CONNECTION_NAME", ""):
    DATABASES["default"]["HOST"] = f"/cloudsql/{connection_name}"
    DATABASES["default"].pop("PORT", None)

# Redis backs the prepare-job progress records. Cloud Run has no sidecar, so
# without REDIS_URL it falls back to per-instance local memory — which is wrong
# the moment Cloud Run runs more than one instance, because a poll can land on
# an instance that never saw the job. Pin max instances to 1, or attach
# Memorystore, before raising concurrency.
if os.environ.get("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles"))
STORAGES = {
    # Declaring STORAGES at all replaces Django's defaults wholesale, so "default"
    # has to be restated — without it every FileField save (application PDFs,
    # uploaded resumes) raises InvalidStorageError.
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # TokenAuthentication is what the native Swift app uses (Authorization: Token <token>,
    # no cookies/CSRF needed). SessionAuthentication is kept only for the Django admin UI.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# Shared secret an external workflow (Apify webhooks, n8n) presents in the
# X-Ingestion-Key header — or a ?key= query param — to push scraped job
# postings into `ingestion`.
INGESTION_API_KEY = os.environ.get("INGESTION_API_KEY", "")

# Shared secret a partner app (TransWell's Jobs tab) presents in the
# X-Partner-Key header to provision an account here for one of its members.
# It can create and delete partner accounts and nothing else — it reads no
# one's data, so leaking it exposes no résumés.
PARTNER_API_KEY = os.environ.get("PARTNER_API_KEY", "")

# Required to read a finished Apify run's dataset. Apify's docs suggest default
# datasets are public, but in practice an unauthenticated GET returns 403 — so
# without this the webhook can't ingest anything.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

# Used to generate tailored application materials. Without it the materials
# endpoint returns a clear "not configured" message rather than failing oddly.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Which account unauthenticated machine callers (the ingestion webhook, Apify)
# attach new rows to — see identity/owners.py. Falls back to "the only user
# in the database" when unset, which is fine today (there is exactly one) but
# would raise a clear error the moment that stops being true.
WORKS_COUT_OWNER_EMAIL = os.environ.get("WORKS_COUT_OWNER_EMAIL", "")

# Google Sheets mirror of the application pipeline. Both must be set for sync to
# run; without them the app logs and carries on rather than failing her action.
# The JSON key is a service-account file — share the sheet with that account's
# email as an Editor, or writes 403 even with a valid key.
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
JOB_SHEET_ID = os.environ.get("JOB_SHEET_ID", "")

# Drive folder the generated application PDFs are mirrored into, so they're
# reachable from an employer's upload dialog rather than only through the API.
# The same service account needs Editor access to the folder.
JOB_DRIVE_FOLDER_ID = os.environ.get("JOB_DRIVE_FOLDER_ID", "")

# Drive uploads run as the user, not as the service account: service accounts
# have no Drive storage quota, so files they create in a consumer account are
# refused. The refresh token is long-lived — it only dies if she revokes access
# or the OAuth consent screen is left unpublished (Testing mode expires it in 7 days).
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")

# The suite is run inside the production container (there's no separate test
# host), so a FileField save in a test would write real files into live media —
# it has done exactly that. Redirect both the path and the storage backend, since
# MEDIA_ROOT alone doesn't move where the default storage writes.
if "test" in sys.argv:
    MEDIA_ROOT = tempfile.mkdtemp(prefix="works-c-out-test-media-")
    STORAGES = {
        **STORAGES,
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": MEDIA_ROOT},
        },
    }
    # Blank every outbound credential too. Redirecting local storage isn't
    # enough: build_documents also uploads to Drive, and the suite duly wrote
    # test-fixture PDFs into her real folder. Anything that reaches Google must
    # be mocked in a test, never live.
    GOOGLE_SERVICE_ACCOUNT_FILE = ""
    JOB_SHEET_ID = ""
    JOB_DRIVE_FOLDER_ID = ""
    GOOGLE_OAUTH_CLIENT_ID = ""
    GOOGLE_OAUTH_CLIENT_SECRET = ""
    GOOGLE_OAUTH_REFRESH_TOKEN = ""

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
