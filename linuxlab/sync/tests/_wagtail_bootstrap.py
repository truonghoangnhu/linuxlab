"""Shared, defensive Wagtail bootstrap helper for this app's tests only.

Mirrors linuxlab/publication/tests/_wagtail_bootstrap.py: a transaction=True
test elsewhere in the full suite can truncate Wagtail's default Locale/root
Page rows, so no test here may assume that bootstrap data survives.
"""

from django.conf import settings
from wagtail.coreutils import get_supported_content_language_variant
from wagtail.models import Locale, Page


def get_or_create_root_page():
    language_code = get_supported_content_language_variant(settings.LANGUAGE_CODE)
    Locale.objects.get_or_create(language_code=language_code)

    root = Page.get_first_root_node()
    if root is None:
        root = Page.add_root(instance=Page(title="Root", slug="root"))
    return root
