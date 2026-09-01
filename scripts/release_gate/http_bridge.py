"""Task 15 Task 7: real HTTP learner-surface bridge for the Node
application actions (T15-D05, T15-D17, T15-D27 S11).

Drives the actual Node HTTP routes (linuxlab.sync.urls/views) exactly as
a real browser would -- a real cookie jar carries the session across the
landing GET, start POST, attempt GET, and submit POST; the CSRF token is
read from the real `csrftoken` cookie the server sets and sent back as
the `X-CSRFToken` header, never bypassed or hand-constructed. No Django
test client is used anywhere in this module; every call is a genuine
socket-level HTTP round trip via `urllib.request`, suitable for driving a
real Gunicorn process. No answer-key material ever appears in a request
or is read from a response here.
"""

import http.cookiejar
import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class ReleaseGateHttpError(Exception):
    """Raised when a release-gate HTTP bridge call receives an
    unexpected status code. Carries the status and response body for
    caller-side diagnosis."""

    def __init__(self, message, status, body):
        super().__init__(message)
        self.status = status
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class LearnerSession:
    """One real cookie-jar-backed learner session against one Node base
    URL -- reused across landing/start/attempt/submit exactly as one
    browser tab would."""

    base_url: str
    cookie_jar: http.cookiejar.CookieJar

    @classmethod
    def start(cls, base_url):
        return cls(base_url=base_url.rstrip("/"), cookie_jar=http.cookiejar.CookieJar())

    def _opener(self, *, follow_redirects=True):
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if not follow_redirects:
            handlers.append(_NoRedirect())
        return urllib.request.build_opener(*handlers)

    def _csrf_token(self):
        for cookie in self.cookie_jar:
            if cookie.name == "csrftoken":
                return cookie.value
        raise ReleaseGateHttpError("no csrftoken cookie present in this session", None, None)

    def get(self, path, *, timeout=10):
        url = self.base_url + path
        try:
            response = self._opener().open(url, timeout=timeout)
            return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def post_form(self, path, *, timeout=10):
        """A form-style POST with no body -- ActivityStartView's actual
        contract: it reads no request body at all."""
        url = self.base_url + path
        request = urllib.request.Request(
            url,
            method="POST",
            headers={"X-CSRFToken": self._csrf_token(), "Referer": url},
            data=b"",
        )
        try:
            response = self._opener(follow_redirects=False).open(request, timeout=timeout)
            location = response.headers.get("Location")
            return response.status, location
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            return exc.code, location

    def post_json(self, path, payload, *, timeout=10):
        url = self.base_url + path
        request = urllib.request.Request(
            url,
            method="POST",
            headers={
                "X-CSRFToken": self._csrf_token(),
                "Referer": url,
                "Content-Type": "application/json",
            },
            data=json.dumps(payload).encode(),
        )
        try:
            response = self._opener().open(request, timeout=timeout)
            return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body)
            except json.JSONDecodeError:
                return exc.code, {"raw_body": body}


def get_landing(session, source_public_id, activity_public_id, version):
    path = f"/learn/{source_public_id}/activity/{activity_public_id}/{version}/"
    return session.get(path)


def post_start(session, source_public_id, activity_public_id, version):
    """Returns (status, attempt_url_path) -- the Location header from the
    303 redirect ActivityStartView issues on success, unparsed beyond
    that (callers combine it with the session's own base_url as needed)."""
    path = f"/learn/{source_public_id}/activity/{activity_public_id}/{version}/start/"
    return session.post_form(path)


def get_attempt(session, attempt_url_path):
    return session.get(attempt_url_path)


def post_submit(session, attempt_url_path, responses):
    """`responses`: [{"question_public_id": ..., "question_version": ...,
    "selected_option_id": ...}, ...] -- the exact AssessmentActivityProvider
    submission shape. Never includes any answer-key/grading field."""
    submit_path = attempt_url_path.rstrip("/") + "/submit/"
    return session.post_json(submit_path, {"responses": responses})
