from django.urls import path

from linuxlab.sync.api import SyncEventsView
from linuxlab.sync.views import LearnView

urlpatterns = [
    path("learn/<uuid:source_public_id>/", LearnView.as_view(), name="learn"),
    path("api/v1/sync/events/", SyncEventsView.as_view(), name="sync-events"),
]
