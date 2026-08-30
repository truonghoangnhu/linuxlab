"""Task 13 Node LAN HTTP adapter (T13-D15).

A thin adapter over linuxlab.runtime: it opens exactly ONE
RuntimePackageContext per request and reuses it for every runtime lookup
needed to render the response, calls only the learner-safe runtime
facade (never linuxlab.runtime.grading), and never queries Installed*
models directly for business/runtime resolution. Domain exceptions from
the runtime core are mapped to a response here -- the runtime core
itself has no HTTP awareness at all.
"""

from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseServerError
from django.shortcuts import render
from django.views import View

from linuxlab.runtime.content import get_runtime_publication
from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.exceptions import (
    RuntimeArtifactNotFound,
    RuntimeContentUnavailable,
    RuntimeContextInvalid,
)

_PACKAGE_TYPE = "learning_path"


class LearnView(View):
    """GET /learn/<uuid:source_public_id>/ -- serves the ACTIVE installed
    learning path for this Node, from local installed package data only.
    """

    def get(self, request, source_public_id):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        try:
            context = open_runtime_context(_PACKAGE_TYPE, source_public_id)
        except RuntimeContentUnavailable:
            return render(request, "sync/learn_unavailable.html", status=503)

        try:
            publication = get_runtime_publication(context)
        except RuntimeArtifactNotFound:
            return render(request, "sync/learn_not_found.html", status=404)
        except RuntimeContextInvalid:
            return HttpResponseServerError("runtime context is no longer valid")

        return render(
            request,
            "sync/learn.html",
            {"runtime_context": context, "publication": publication},
            status=200,
        )
