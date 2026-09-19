from django.urls import path

from linuxlab.sync.api import SyncEventsView, SyncProbeView
from linuxlab.sync.views import (
    ActivityLandingView,
    ActivityStartView,
    AttemptSubmitView,
    AttemptView,
    LearnView,
)

urlpatterns = [
    # T15-D27 S11 corrective: an explicit Start -> Attempt -> Submit flow
    # (not a single atomic-POST landing route) so a real Attempt only
    # ever exists once a POST to /start/ actually created one, and
    # rendering/submitting an existing Attempt never re-resolves "the
    # current active package". These routes must precede the bare lesson
    # route below so "activity/"/"attempt/" are never swallowed as an
    # opaque source_public_id path segment (all are registered on the
    # same "learn/" prefix; Django tries patterns in order).
    path(
        "learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/",
        ActivityLandingView.as_view(),
        name="learn-activity",
    ),
    path(
        "learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/start/",
        ActivityStartView.as_view(),
        name="learn-activity-start",
    ),
    path("learn/attempt/<uuid:attempt_public_id>/", AttemptView.as_view(), name="learn-attempt"),
    path(
        "learn/attempt/<uuid:attempt_public_id>/submit/",
        AttemptSubmitView.as_view(),
        name="learn-attempt-submit",
    ),
    path("learn/<uuid:source_public_id>/", LearnView.as_view(), name="learn"),
    path("api/v1/sync/events/", SyncEventsView.as_view(), name="sync-events"),
    # Corrective B Task 11: dedicated read-only Central probe, beside the
    # events route and ahead of wagtail's catch-all in the project urls.
    path("api/v1/sync/probe/", SyncProbeView.as_view(), name="sync-probe"),
]
