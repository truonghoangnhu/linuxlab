from django.db import transaction
from django.utils import timezone

from linuxlab.activities.models import ActivityDefinition, ActivityVersion


def create_activity_version(definition, config=None, required_capabilities=None):
    config = {} if config is None else config
    required_capabilities = [] if required_capabilities is None else required_capabilities

    with transaction.atomic():
        locked_definition = ActivityDefinition.objects.select_for_update().get(pk=definition.pk)
        current_max = (
            ActivityVersion.objects.filter(definition=locked_definition)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        next_version = (current_max or 0) + 1
        return ActivityVersion.objects.create(
            definition=locked_definition,
            version=next_version,
            config=config,
            required_capabilities=required_capabilities,
        )


def publish_activity_version(activity_version, registry):
    # The row lock must be acquired before checking published state, otherwise
    # two concurrent publishers can both observe "unpublished" and both
    # succeed. The locked, persisted row (not the caller's possibly-stale
    # object) is what gets validated and published.
    with transaction.atomic():
        locked_version = (
            ActivityVersion.objects.select_for_update()
            .select_related("definition")
            .get(pk=activity_version.pk)
        )
        if locked_version.is_published:
            raise ValueError("published ActivityVersion is immutable")

        provider = registry.get(locked_version.definition.type_key)
        provider.validate_config(locked_version.config)

        locked_version.published_at = timezone.now()
        locked_version.save()
        return locked_version


def get_activity_version(public_id, version):
    return ActivityVersion.objects.get(
        definition__public_id=public_id,
        version=version,
        published_at__isnull=False,
    )
