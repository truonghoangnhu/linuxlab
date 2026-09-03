"""Task 15 Task 11: release-gate target safety -- the gate must refuse
to operate against a normal dev/prod database or arbitrary pre-existing
state, and must only ever accept the exact ephemeral names it itself
generated for the current run.

This is enforced structurally, not by an extra runtime check Task 11
adds: scripts.release_gate.database.create_database()/drop_database()
already accept ONLY a real `GateDatabaseNames` object plus a role
selector -- there is no code path anywhere in the release gate
(dual_deployment(), scenarios.py, gate_runner.py, phase1_release_gate.py)
that can supply a bare database-name string, so `linuxlab_central_dev`,
`linuxlab_node_dev`, a production-like name, or an arbitrary
pre-existing database can never reach a connection. That refusal
behavior itself is exhaustively covered by
scripts/tests/test_release_gate_database.py (e.g.
test_create_database_refuses_production_like_string,
test_create_database_refuses_embedded_dev_name_string,
test_create_database_refuses_foreign_run_looking_string,
test_create_database_refuses_prefix_only_string) -- not duplicated
here. These tests instead prove the release-gate-level GUARANTEE that
makes those lower-level refusals unreachable in normal operation: every
name the gate ever generates is canonically derived from a fresh
run_id and can never coincide with a known dev/prod name.
"""

import re

from scripts.release_gate.database import GateDatabaseNames, assert_ephemeral_database_name
from scripts.release_gate.environment import generate_run_id

_KNOWN_DEV_DATABASES = ("linuxlab_central_dev", "linuxlab_node_dev", "linuxlab_test")
_KNOWN_PRODUCTION_LIKE_PATTERN = re.compile(r"^linuxlab_(central|node)_prod")


def test_generated_run_ids_never_collide_with_a_known_dev_database_name():
    for _ in range(200):
        run_id = generate_run_id()
        assert run_id not in _KNOWN_DEV_DATABASES
        assert not _KNOWN_PRODUCTION_LIKE_PATTERN.match(run_id)


def test_generated_gate_database_names_never_equal_a_known_dev_or_prod_name():
    for _ in range(50):
        names = GateDatabaseNames.for_run(generate_run_id())
        for name in names.all_names():
            assert name not in _KNOWN_DEV_DATABASES
            assert not _KNOWN_PRODUCTION_LIKE_PATTERN.match(name)
            # Accepted by the gate's own ephemeral-name safety check --
            # the same check create_database()/drop_database() enforce
            # before ever opening a connection.
            assert_ephemeral_database_name(name)


def test_generated_gate_database_names_are_prefixed_and_role_scoped():
    names = GateDatabaseNames.for_run(generate_run_id())
    assert names.central.startswith("linuxlab_gate_central_")
    assert names.node.startswith("linuxlab_gate_node_")
    assert names.restore_central.startswith("linuxlab_gate_restore_central_")
    assert names.restore_node.startswith("linuxlab_gate_restore_node_")
    # Never a bare prefix with nothing appended (would be as unsafe as
    # a wildcard target) -- assert_ephemeral_database_name() itself
    # requires content after the prefix; this proves for_run() satisfies
    # that on the real generator, not just in isolation.
    for name in names.all_names():
        assert_ephemeral_database_name(name)
