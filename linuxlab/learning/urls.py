from django.urls import path

from linuxlab.learning.views import ClaimProgressView

urlpatterns = [
    path("claim-progress/", ClaimProgressView.as_view(), name="claim-progress"),
]
