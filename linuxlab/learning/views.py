import json

from django.http import JsonResponse
from django.views import View

from linuxlab.learning.services import claim_guest_progress


class ClaimProgressView(View):
    """POST /api/v1/learning/claim-progress/ (Task 08, ADR-006/ADR-012).

    Authenticated Accounts only submit an explicit list of guest-local
    completions to claim; anything not literally present in the request
    body is never touched. See claim_guest_progress() for the validation
    and idempotency contract.
    """

    def post(self, request):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "authentication required"}, status=401)

        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "invalid JSON body"}, status=400)

        claims = payload.get("claims") if isinstance(payload, dict) else None

        try:
            learner, results = claim_guest_progress(request.user, claims)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        return JsonResponse(
            {
                "learner_public_id": str(learner.public_id),
                "claimed": len(results),
            },
            status=200,
        )
