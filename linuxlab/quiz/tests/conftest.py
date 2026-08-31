"""T15-D27: re-exposes linuxlab.runtime.tests's installed_active_world
fixture (a real Task 09-12 chain, staged AND activated) for
serialize_installed_assessment_for_learner()/evaluate(context=...) tests."""

from linuxlab.runtime.tests.conftest import installed_active_world  # noqa: F401
