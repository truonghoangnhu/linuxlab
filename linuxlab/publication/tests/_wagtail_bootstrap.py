"""Shared, defensive Wagtail bootstrap helper for this app's tests only.

A transaction=True test elsewhere in the full suite (e.g. activities'
concurrent-publication test) truncates all tables, including Wagtail's
default Locale and root Page rows -- both seeded by one-time data
migrations, not recreated by Django's post-flush signals. Content app tests
happen to run earlier in the suite's collection order and never observe
this, but this app's own transaction=True test can run after such a
truncation, so no test here may assume Wagtail's bootstrap data survives.
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
