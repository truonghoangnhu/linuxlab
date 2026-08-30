from django.contrib import admin
from django.urls import include, path
from wagtail import urls as wagtail_urls
from wagtail.admin import urls as wagtailadmin_urls
from wagtail.documents import urls as wagtaildocs_urls

from linuxlab.core.health import healthz, readyz

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("admin/", include(wagtailadmin_urls)),
    path("documents/", include(wagtaildocs_urls)),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("api/v1/learning/", include("linuxlab.learning.urls")),
    # Task 13: must precede wagtail_urls' catch-all page-serving route below.
    path("", include("linuxlab.sync.urls")),
    path("", include(wagtail_urls)),
]
