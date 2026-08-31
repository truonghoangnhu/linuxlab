"""Task 13 Node LAN HTTP adapter (T13-D15), extended by T15-D27 S11 with
the Node learner activity/submit surface.

A thin adapter over linuxlab.runtime and linuxlab.learning: it opens
exactly ONE RuntimePackageContext per request and reuses it for every
runtime lookup needed to render/grade the response, calls only the
learner-safe runtime facade for rendering (never linuxlab.runtime.grading
directly -- grading is reached only through
linuxlab.learning.services.submit_installed_attempt(), which itself owns
that boundary), and never queries Installed*/Attempt models directly for
business/runtime resolution. Domain exceptions from the runtime/learning
core are mapped to a response here -- neither core has any HTTP awareness
at all.
"""

import json

from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseServerError, JsonResponse
from django.shortcuts import render
from django.views import View

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.identity.services import ensure_learner, resolve_or_create_guest_learner
from linuxlab.learning.services import start_installed_attempt, submit_installed_attempt
from linuxlab.quiz.provider import serialize_installed_assessment_for_learner
from linuxlab.runtime.content import get_runtime_activity, get_runtime_publication
from linuxlab.runtime.context import open_runtime_context
from linuxlab.runtime.exceptions import (
    RuntimeArtifactNotFound,
    RuntimeContentUnavailable,
    RuntimeContextInvalid,
)

_PACKAGE_TYPE = "learning_path"
_LEARNER_SESSION_KEY = "linuxlab_learner_public_id"


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


def _resolve_node_learner(request):
    """Resolve (or mint) the Learner identity for a Node HTTP
    request (T15-D27 S11, corrective identity-boundary finding).

    This view owns only session binding and request orchestration; actual
    Learner resolution/creation is entirely delegated to the public
    linuxlab.identity services -- never a direct Learner.objects.create()/
    filter() call here.

    An authenticated request resolves through the existing public
    ensure_learner() service (ADR-006/ADR-012), unchanged from the Task 08
    Central claim flow. An anonymous request resolves through
    resolve_or_create_guest_learner(), keyed by whatever public_id this
    same session previously stored (never a caller-posted value) so a
    GET-then-POST from the same browser reuses one identity rather than
    minting a new Learner per request. Node classroom practice is
    local-first learning (architecture spec S5.2/S11.1): unlike Central's
    client-side-only guest quiz flow (Task 08), a Node installed Attempt
    is always a real server-side record -- that IS what "Node execution
    source" means -- so an anonymous learner still needs a real local
    Learner row to attach it to.
    """
    if request.user.is_authenticated:
        return ensure_learner(request.user)

    learner = resolve_or_create_guest_learner(public_id=request.session.get(_LEARNER_SESSION_KEY))
    request.session[_LEARNER_SESSION_KEY] = str(learner.public_id)
    return learner


class ActivityView(View):
    """GET /learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/
    -- serves the learner-safe payload for one exact installed Activity
    (T15-D27 S11): ACTIVE InstalledPackage -> RuntimePackageContext ->
    InstalledActivityVersion -> learner-safe provider payload. Never
    exposes grading material.
    """

    def get(self, request, source_public_id, activity_public_id, version):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        try:
            context = open_runtime_context(_PACKAGE_TYPE, source_public_id)
        except RuntimeContentUnavailable:
            return render(request, "sync/learn_unavailable.html", status=503)

        try:
            activity = get_runtime_activity(context, activity_public_id, version)
        except RuntimeArtifactNotFound:
            return render(request, "sync/learn_not_found.html", status=404)
        except RuntimeContextInvalid:
            return HttpResponseServerError("runtime context is no longer valid")

        activity_registry = build_default_activity_registry()
        try:
            provider = activity_registry.get(activity.type_key)
        except LookupError:
            return HttpResponseServerError("no provider registered for this activity type")

        payload = serialize_installed_assessment_for_learner(context, activity, provider.question_registry)

        return render(
            request,
            "sync/learn_activity.html",
            {
                "runtime_context": context,
                "activity": activity,
                "payload": payload,
                "submit_url": request.path.rstrip("/") + "/submit/",
            },
            status=200,
        )


class ActivitySubmitView(View):
    """POST /learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/submit/
    -- starts a fresh installed Attempt against the exact pinned activity
    and immediately grades it (T15-D27 S6, S8, S11): Attempt -> pinned
    InstalledActivityVersion -> pinned package context -> installed
    grading -> Result -> Progress -> activity.completed. Never
    re-resolves "the active package" between start and grade -- both
    happen inside this one request against the SAME RuntimePackageContext.
    """

    def post(self, request, source_public_id, activity_public_id, version):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        try:
            submission = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "invalid JSON body"}, status=400)

        try:
            context = open_runtime_context(_PACKAGE_TYPE, source_public_id)
        except RuntimeContentUnavailable:
            return JsonResponse({"error": "not available"}, status=503)

        learner = _resolve_node_learner(request)
        activity_registry = build_default_activity_registry()

        try:
            attempt = start_installed_attempt(learner, context, activity_public_id, version)
        except RuntimeArtifactNotFound:
            return JsonResponse({"error": "not found"}, status=404)
        except RuntimeContextInvalid:
            return HttpResponseServerError("runtime context is no longer valid")
        except ValueError:
            return JsonResponse({"error": "package is not active"}, status=409)

        try:
            result = submit_installed_attempt(attempt, submission, activity_registry)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        return JsonResponse(
            {
                "attempt_public_id": str(attempt.public_id),
                "score": result.score,
                "max_score": result.max_score,
            },
            status=200,
        )
