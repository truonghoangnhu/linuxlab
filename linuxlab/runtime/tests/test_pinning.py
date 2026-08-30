"""Task 13 end-to-end proof of request/use-case pinning (T13-D08, T13-D10,
T13-D11, T13-D14) and of the runtime core's independence from Central/
Wagtail/authoring sources (T13-D01)."""

from pathlib import Path

import pytest
from django.test import override_settings

from linuxlab.publication.package import build_package
from linuxlab.runtime import content as content_module
from linuxlab.runtime import context as context_module
from linuxlab.runtime import grading as grading_module
from linuxlab.runtime.content import get_runtime_activity, get_runtime_publication
from linuxlab.runtime.context import open_runtime_context
from linuxlab.sync.models import InstalledActivityVersion
from linuxlab.sync.services import activate_package, import_package

from .conftest import republish

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_one_pinned_context_never_mixes_two_package_versions(installed_active_world):
    """v1 ACTIVE -> open ctx -> read publication v1 -> activate v2 ->
    ctx still reads v1's activity -> a NEW context resolves v2. The
    original context is never reopened or auto-switched (T13-D08, D14)."""
    world = installed_active_world
    registry = world["activity_registry"]
    private_key, public_key = world["private_key"], world["public_key"]
    v1 = world["package"]
    v1_activity = InstalledActivityVersion.objects.get(package=v1)

    ctx = open_runtime_context(v1.package_type, v1.source_public_id)
    pub_v1 = get_runtime_publication(ctx)
    assert pub_v1.version == world["snapshot"].version

    v2_snapshot = republish(world)
    v2 = import_package(build_package(v2_snapshot, registry, private_key), registry, public_key)
    activate_package(v2)

    # The same, already-opened context still resolves v1's activity.
    activity_from_old_ctx = get_runtime_activity(
        ctx, v1_activity.activity_public_id, v1_activity.version
    )
    assert activity_from_old_ctx.version == v1_activity.version

    # A brand-new context resolves v2, never reusing/mutating the old one.
    new_ctx = open_runtime_context(v1.package_type, v1.source_public_id)
    assert new_ctx.package_pk == v2.pk
    assert new_ctx.package_pk != ctx.package_pk


@override_settings(LINUXLAB_CAN_IMPORT_PACKAGES=True)
def test_runtime_still_resolves_content_after_deleting_its_original_wagtail_and_snapshot_source(
    installed_active_world,
):
    """Direct behavioral proof of independence: delete the Wagtail page
    and the PublicationSnapshot that originally produced this package,
    and confirm the runtime still resolves content purely from the
    retained Task 12 installed artifacts."""
    from linuxlab.content.models import LearningPathPage
    from linuxlab.publication.models import PublicationSnapshot

    world = installed_active_world
    package = world["package"]
    ctx = open_runtime_context(package.package_type, package.source_public_id)

    LearningPathPage.objects.filter(public_id=world["snapshot"].source_public_id).delete()
    PublicationSnapshot.objects.all().delete()

    publication = get_runtime_publication(ctx)
    assert publication.source_public_id == package.source_public_id


_FORBIDDEN_SOURCE_PHRASES = (
    "import wagtail",
    "from wagtail",
    "PublicationSnapshot",
    "from linuxlab.activities.models import ActivityVersion",
    "from linuxlab.assessment.models import QuestionVersion",
    "from linuxlab.content.models import LearningPathPage",
)


def test_runtime_core_source_never_references_central_or_wagtail_fallback_sources():
    """Source-inspection assertion (preferred over brittle global
    monkeypatching): the runtime-core modules must never even import a
    Central/Wagtail/domain fallback source, regardless of whether a test
    happens to exercise the code path that would use it."""
    for module in (content_module, context_module, grading_module):
        source = Path(module.__file__).read_text()
        for phrase in _FORBIDDEN_SOURCE_PHRASES:
            assert phrase not in source, f"{module.__name__} references forbidden {phrase!r}"
