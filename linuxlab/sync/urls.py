from django.urls import path

from linuxlab.sync.views import LearnView

urlpatterns = [
    path("learn/<uuid:source_public_id>/", LearnView.as_view(), name="learn"),
]
