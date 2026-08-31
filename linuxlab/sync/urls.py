from django.urls import path

from linuxlab.sync.api import SyncEventsView
from linuxlab.sync.views import ActivitySubmitView, ActivityView, LearnView

urlpatterns = [
    # T15-D27 S11: activity/submit routes must precede the bare lesson
    # route below so "activity/" is never swallowed as an opaque
    # source_public_id path segment (both are registered on the same
    # "learn/" prefix; Django tries patterns in order).
    path(
        "learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/",
        ActivityView.as_view(),
        name="learn-activity",
    ),
    path(
        "learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/submit/",
        ActivitySubmitView.as_view(),
        name="learn-activity-submit",
    ),
    path("learn/<uuid:source_public_id>/", LearnView.as_view(), name="learn"),
    path("api/v1/sync/events/", SyncEventsView.as_view(), name="sync-events"),
]
