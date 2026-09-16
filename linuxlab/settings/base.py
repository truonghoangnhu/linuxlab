import os
from pathlib import Path

from linuxlab.sync.configuration import parse_sync_setting

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-local-development-only")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",
    "modelcluster",
    "taggit",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "linuxlab.core",
    "linuxlab.identity",
    "linuxlab.content",
    "linuxlab.activities",
    "linuxlab.assessment",
    "linuxlab.quiz",
    "linuxlab.learning",
    "linuxlab.publication",
    "linuxlab.catalog",
    "linuxlab.sync",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
]

ROOT_URLCONF = "linuxlab.urls"

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

AUTH_USER_MODEL = "identity.Account"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

WAGTAIL_SITE_NAME = "LinuxLab"
WAGTAILADMIN_BASE_URL = os.environ.get("WAGTAILADMIN_BASE_URL", "http://localhost:8000")

# Profile flags; each deployment profile module overrides these explicitly.
LINUXLAB_CAN_AUTHOR = False
LINUXLAB_CAN_IMPORT_PACKAGES = False
LINUXLAB_CAN_SERVE_LEARNER_CONTENT = False

# Task 14 corrective (independent review finding #1): Central-only sync
# authority (ingestion API + Node credential provisioning) and Node-only
# outbound worker authority are each a fixed deployment-profile capability,
# the same as LINUXLAB_CAN_AUTHOR/LINUXLAB_CAN_IMPORT_PACKAGES/
# LINUXLAB_CAN_SERVE_LEARNER_CONTENT above -- never a LINUXLAB_PROFILE
# string comparison scattered across call sites.
LINUXLAB_CAN_RECEIVE_SYNC_EVENTS = False
LINUXLAB_CAN_RUN_SYNC_WORKER = False

# Task 14: Node -> Central event sync configuration. Sync-enablement is
# per-Node-instance runtime configuration (not a fixed profile capability
# like LINUXLAB_CAN_*), so it is read from the environment with a safe
# default of "disabled" everywhere, including Node -- a Node must be
# explicitly provisioned and configured before it enqueues syncable
# events (T14-D38).
LINUXLAB_SYNC_ENABLED = parse_sync_setting(
    "LINUXLAB_SYNC_ENABLED", os.environ.get("LINUXLAB_SYNC_ENABLED", "false")
)
LINUXLAB_NODE_ID = os.environ.get("LINUXLAB_NODE_ID") or None

# Task 14 Node-side transport/worker configuration (T14-D01, T14-D02,
# T14-D16, T14-D17). The bearer secret lives in environment/local secret
# configuration only -- never in the database (T14-D18).
LINUXLAB_SYNC_SECRET = os.environ.get("LINUXLAB_SYNC_SECRET") or None
LINUXLAB_CENTRAL_URL = os.environ.get("LINUXLAB_CENTRAL_URL") or None
LINUXLAB_SYNC_ALLOW_INSECURE_HTTP = parse_sync_setting(
    "LINUXLAB_SYNC_ALLOW_INSECURE_HTTP",
    os.environ.get("LINUXLAB_SYNC_ALLOW_INSECURE_HTTP", "false"),
)
LINUXLAB_SYNC_BATCH_MAX_EVENTS = parse_sync_setting(
    "LINUXLAB_SYNC_BATCH_MAX_EVENTS", os.environ.get("LINUXLAB_SYNC_BATCH_MAX_EVENTS", "100")
)
LINUXLAB_SYNC_BATCH_MAX_BYTES = parse_sync_setting(
    "LINUXLAB_SYNC_BATCH_MAX_BYTES", os.environ.get("LINUXLAB_SYNC_BATCH_MAX_BYTES", "1000000")
)
LINUXLAB_SYNC_LEASE_SECONDS = parse_sync_setting(
    "LINUXLAB_SYNC_LEASE_SECONDS", os.environ.get("LINUXLAB_SYNC_LEASE_SECONDS", "300")
)
LINUXLAB_SYNC_BACKOFF_BASE_SECONDS = parse_sync_setting(
    "LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", os.environ.get("LINUXLAB_SYNC_BACKOFF_BASE_SECONDS", "5")
)
LINUXLAB_SYNC_BACKOFF_CAP_SECONDS = parse_sync_setting(
    "LINUXLAB_SYNC_BACKOFF_CAP_SECONDS", os.environ.get("LINUXLAB_SYNC_BACKOFF_CAP_SECONDS", "3600")
)
LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS = parse_sync_setting(
    "LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS", os.environ.get("LINUXLAB_SYNC_RETRY_MAX_ATTEMPTS", "10")
)
LINUXLAB_SYNC_RETRY_MAX_AGE = parse_sync_setting(
    "LINUXLAB_SYNC_RETRY_MAX_AGE", os.environ.get("LINUXLAB_SYNC_RETRY_MAX_AGE", "86400")
)
LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE = parse_sync_setting(
    "LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE",
    os.environ.get("LINUXLAB_REQUIRE_CENTRAL_PROBE_FOR_RELEASE", "false"),
)


def database_config(default_name):
    """Build a PostgreSQL DATABASES entry for a deployment profile.

    Each profile module supplies its own default database name so Central,
    Node, and test never accidentally share one database (ADR-007).
    """
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", default_name),
        "USER": os.environ.get("POSTGRES_USER", "linuxlab"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
