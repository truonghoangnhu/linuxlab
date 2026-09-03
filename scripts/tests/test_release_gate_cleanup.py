"""Task 15 Tasks 7-10 final cleanup corrective: cleanup-action
aggregation and deployment-owned workspace removal primitives.

run_cleanup_actions() attempts every registered action exactly once,
regardless of whether an earlier one failed -- e.g. Node database
cleanup failing must never skip Central database cleanup -- and raises
a single aggregated error naming every action that failed, only after
all have been attempted.
"""

import pytest

from scripts.release_gate.cleanup import (
    CleanupAggregateError,
    remove_workspace,
    run_cleanup_actions,
)


def test_run_cleanup_actions_runs_every_action_when_all_succeed():
    order = []
    run_cleanup_actions(
        [
            ("first", lambda: order.append("first")),
            ("second", lambda: order.append("second")),
            ("third", lambda: order.append("third")),
        ]
    )
    assert order == ["first", "second", "third"]


def test_run_cleanup_actions_attempts_every_action_even_when_earlier_ones_fail():
    """The hard invariant: cleanup(node_db) failing must never skip
    cleanup(central_db). Every action runs regardless of prior failures."""
    attempted = []

    def _fail(name):
        attempted.append(name)
        raise RuntimeError(f"{name} failed")

    def _succeed(name):
        attempted.append(name)

    with pytest.raises(CleanupAggregateError):
        run_cleanup_actions(
            [
                ("stop_node_process", lambda: _fail("stop_node_process")),
                ("stop_central_process", lambda: _succeed("stop_central_process")),
                ("remove_workspace", lambda: _fail("remove_workspace")),
                ("drop_node_database", lambda: _fail("drop_node_database")),
                ("drop_central_database", lambda: _succeed("drop_central_database")),
            ]
        )

    assert attempted == [
        "stop_node_process",
        "stop_central_process",
        "remove_workspace",
        "drop_node_database",
        "drop_central_database",
    ]


def test_run_cleanup_actions_error_names_every_failed_action():
    with pytest.raises(CleanupAggregateError) as excinfo:
        run_cleanup_actions(
            [
                ("drop_node_database", lambda: (_ for _ in ()).throw(RuntimeError("node db still busy"))),
                ("drop_central_database", lambda: None),
            ]
        )
    message = str(excinfo.value)
    assert "drop_node_database" in message
    assert "node db still busy" in message
    assert len(excinfo.value.failures) == 1
    assert excinfo.value.failures[0][0] == "drop_node_database"


def test_run_cleanup_actions_raises_nothing_when_no_actions_given():
    run_cleanup_actions([])  # must not raise


# --- remove_workspace ---------------------------------------------------


def test_remove_workspace_removes_a_populated_directory_tree(tmp_path):
    workspace = tmp_path / "gate-workspace-abc"
    (workspace / "nested").mkdir(parents=True)
    (workspace / "file.txt").write_text("artifact")
    (workspace / "nested" / "deep.txt").write_text("deeper artifact")

    remove_workspace(workspace)

    assert not workspace.exists()


def test_remove_workspace_is_a_safe_no_op_when_already_absent(tmp_path):
    workspace = tmp_path / "never-created-workspace"
    remove_workspace(workspace)  # must not raise
    remove_workspace(workspace)  # calling twice must also not raise


def test_remove_workspace_never_touches_the_parent_directory(tmp_path):
    workspace = tmp_path / "gate-workspace-xyz"
    workspace.mkdir()
    sibling = tmp_path / "unrelated-user-file.txt"
    sibling.write_text("must survive")

    remove_workspace(workspace)

    assert not workspace.exists()
    assert sibling.exists()
    assert tmp_path.exists()
