"""Task 13 Node LAN HTTP adapter (T13-D15), extended by T15-D27 S11 with
the Node learner activity/start/attempt/submit surface, and refined by the
T15-D27 corrective into an explicit Start -> Attempt -> Submit flow.

A thin adapter over linuxlab.runtime and linuxlab.learning: it opens
exactly ONE RuntimePackageContext per request and reuses it for every
runtime lookup needed to render/grade the response, calls only the
learner-safe runtime facade for rendering (never linuxlab.runtime.grading
directly -- grading is reached only through
linuxlab.learning.services.submit()/submit_installed_attempt(), which
themselves own that boundary), and never queries Installed*/Attempt
models directly for business/runtime resolution -- Attempt lookup goes
through linuxlab.learning.services.get_attempt_by_public_id(), Learner
resolution through linuxlab.identity.services. Domain exceptions from the
runtime/learning core are mapped to a response here -- neither core has
any HTTP awareness at all.
"""

import json

from django.conf import settings
from django.http import (
    HttpResponseForbidden,
    HttpResponseRedirect,
    HttpResponseServerError,
    JsonResponse,
)
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from linuxlab.activities.default_registry import build_default_activity_registry
from linuxlab.identity.services import (
    ensure_learner,
    resolve_existing_guest_learner,
    resolve_existing_learner_for_account,
    resolve_or_create_guest_learner,
)
from linuxlab.learning.services import get_attempt_by_public_id, start_installed_attempt, submit
from linuxlab.quiz.provider import serialize_installed_assessment_for_learner
from linuxlab.runtime.content import get_runtime_activity, get_runtime_publication
from linuxlab.runtime.context import open_pinned_context, open_runtime_context
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

    CREATE-CAPABLE: reserved for ActivityStartView (POST /start/) only,
    which is the one action that legitimately begins a new local
    identity. Every other view authorizing access to an ALREADY-EXISTING
    Attempt (AttemptView, AttemptSubmitView) must use the fully
    read-only _resolve_existing_node_learner() below instead -- a denied
    request must never create a Learner or a session as a side effect
    (T15-D27 final corrective, read-only-authorization finding).

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


def _resolve_existing_node_learner(request):
    """Fully READ-ONLY learner resolution for authorizing access to an
    ALREADY-EXISTING Attempt (T15-D27 final corrective, read-only-
    authorization finding): never creates a Learner, never writes to
    `request.session`. Returns None if this request presents no
    already-established identity at all -- callers must then deny (an
    absent identity can never be treated as "no restriction").

    Used by AttemptView (GET) and AttemptSubmitView (POST) only; POST
    /start/ keeps using the create-capable _resolve_node_learner() above,
    since starting a new Attempt is the one action that legitimately
    begins a new local identity.
    """
    if request.user.is_authenticated:
        return resolve_existing_learner_for_account(request.user)

    return resolve_existing_guest_learner(public_id=request.session.get(_LEARNER_SESSION_KEY))


def _resolve_activity_registry_provider(activity):
    """Shared by every view below that needs a learner-safe serializer:
    resolve the registered ActivityProvider for `activity.type_key` from
    the one production registry (T15-D27 S10), or None if none is
    registered."""
    activity_registry = build_default_activity_registry()
    try:
        return activity_registry.get(activity.type_key)
    except LookupError:
        return None


class ActivityLandingView(View):
    """GET /learn/<uuid:source_public_id>/activity/<uuid:activity_public_id>/<int:version>/
    -- landing page for one exact installed Activity (T15-D27 corrective):
    renders learner-safe content plus a Start action. Never creates an
    Attempt here and never accepts a caller-posted learner identity --
    that is entirely ActivityStartView's job, resolved from session/auth
    state only.
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

        provider = _resolve_activity_registry_provider(activity)
        if provider is None:
            return HttpResponseServerError("no provider registered for this activity type")

        payload = serialize_installed_assessment_for_learner(context, activity, provider.question_registry)

        return render(
            request,
            "sync/learn_activity_landing.html",
            {
                "runtime_context": context,
                "activity": activity,
                "payload": payload,
                "start_url": reverse(
                    "learn-activity-start", args=[source_public_id, activity_public_id, version]
                ),
            },
            status=200,
        )


class ActivityStartView(View):
    """POST .../start/ -- starts (and pins) a fresh installed Attempt
    against the CURRENT active package for this activity (T15-D27
    corrective), then redirects (303 See Other) to that Attempt's own
    page. Accepts no meaningful request body: starting an attempt carries
    no answers/grading, and the learner is resolved entirely from
    session/auth state via the identity service -- never from anything
    posted in the request.
    """

    def post(self, request, source_public_id, activity_public_id, version):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        try:
            context = open_runtime_context(_PACKAGE_TYPE, source_public_id)
        except RuntimeContentUnavailable:
            return JsonResponse({"error": "not available"}, status=503)

        learner = _resolve_node_learner(request)

        try:
            attempt = start_installed_attempt(learner, context, activity_public_id, version)
        except RuntimeArtifactNotFound:
            return JsonResponse({"error": "not found"}, status=404)
        except RuntimeContextInvalid:
            return HttpResponseServerError("runtime context is no longer valid")
        except ValueError:
            return JsonResponse({"error": "package is not active"}, status=409)

        return HttpResponseRedirect(reverse("learn-attempt", args=[attempt.public_id]), status=303)


class AttemptView(View):
    """GET /learn/attempt/<uuid:attempt_public_id>/ -- renders the
    learner-safe payload for an ALREADY-STARTED installed Attempt,
    resolved entirely from the Attempt's own pinned
    InstalledActivityVersion/package (T15-D27 corrective) via
    open_pinned_context() -- never open_runtime_context(), so this page
    keeps rendering correctly even after a different package has since
    become ACTIVE for this same source.
    """

    def get(self, request, attempt_public_id):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        attempt = get_attempt_by_public_id(attempt_public_id)
        if attempt is None or attempt.installed_activity_version_id is None:
            return render(request, "sync/learn_not_found.html", status=404)

        learner = _resolve_existing_node_learner(request)
        if learner is None or learner.pk != attempt.learner_id:
            return HttpResponseForbidden("this Attempt does not belong to the current session")

        installed_activity = attempt.installed_activity_version
        context = open_pinned_context(installed_activity.package)

        try:
            activity = get_runtime_activity(
                context, installed_activity.activity_public_id, installed_activity.version
            )
        except RuntimeArtifactNotFound:
            return HttpResponseServerError("pinned installed activity is missing its own package data")

        provider = _resolve_activity_registry_provider(activity)
        if provider is None:
            return HttpResponseServerError("no provider registered for this activity type")

        payload = serialize_installed_assessment_for_learner(context, activity, provider.question_registry)

        return render(
            request,
            "sync/learn_attempt.html",
            {
                "attempt": attempt,
                "runtime_context": context,
                "activity": activity,
                "payload": payload,
                "submit_url": reverse("learn-attempt-submit", args=[attempt.public_id]),
                "already_submitted": attempt.submitted_at is not None,
            },
            status=200,
        )


class AttemptSubmitView(View):
    """POST /learn/attempt/<uuid:attempt_public_id>/submit/ -- grades an
    ALREADY-STARTED Attempt (T15-D27 corrective): never creates a new
    Attempt and never re-resolves "the current active package" (that is
    submit()/submit_installed_attempt()'s own pinned-context guarantee).
    Rejects a submitting session/identity that does not match the
    Attempt's own learner; any learner-identifying field in the POST body
    itself is never read at all -- only the session/auth-resolved
    identity is ever trusted.
    """

    def post(self, request, attempt_public_id):
        if not getattr(settings, "LINUXLAB_CAN_SERVE_LEARNER_CONTENT", False):
            return HttpResponseForbidden(
                "Node learner content serving is not enabled on this deployment profile"
            )

        try:
            submission = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "invalid JSON body"}, status=400)

        attempt = get_attempt_by_public_id(attempt_public_id)
        if attempt is None or attempt.installed_activity_version_id is None:
            return JsonResponse({"error": "not found"}, status=404)

        learner = _resolve_existing_node_learner(request)
        if learner is None or learner.pk != attempt.learner_id:
            return JsonResponse({"error": "forbidden"}, status=403)

        activity_registry = build_default_activity_registry()
        try:
            result = submit(attempt, submission, activity_registry)
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
