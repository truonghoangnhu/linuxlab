"""T15-D27: re-exposes linuxlab.runtime.tests's installed_active_world
fixture (a real Task 09-12 chain, staged AND activated) so learning/tests
can build Node-installed Attempts without duplicating that ~140-line
bootstrap. conftest.py fixtures are auto-discovered by pytest, so this
avoids the F811 "redefined" lint noise an inline cross-module import would
trigger at every test function using it as a parameter.
"""

from linuxlab.runtime.tests.conftest import installed_active_world  # noqa: F401
