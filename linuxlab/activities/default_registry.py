"""T15-D27 S10, S13: the one production ActivityRegistry builder, used by
ordinary runtime/service code and the Release E2E alike -- not a pytest
fixture, not a release-gate-only construct.

Section 13 of the T15-D27 amendment assigns "activities / assessment own
provider definitions and production registry construction" to this pair of
modules jointly: assessment owns building its own QuestionRegistry
(default_registry.build_default_question_registry()); activities owns
assembling the ActivityRegistry that wires the (currently sole) production
ActivityProvider -- AssessmentActivityProvider, implemented in
linuxlab.quiz -- on top of it.
"""

from linuxlab.activities.registry import ActivityRegistry
from linuxlab.assessment.default_registry import build_default_question_registry
from linuxlab.quiz.provider import AssessmentActivityProvider


def build_default_activity_registry():
    registry = ActivityRegistry()
    registry.register(AssessmentActivityProvider(build_default_question_registry()))
    return registry
