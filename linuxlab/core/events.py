from linuxlab.core.models import DomainEvent


def publish_event(event_type, payload, aggregate_id):
    return DomainEvent.objects.create(
        event_type=event_type,
        payload=payload,
        aggregate_id=aggregate_id,
    )
