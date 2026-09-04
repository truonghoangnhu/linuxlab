"""Task 15 Task 11 corrective: dual_deployment(run_id=...) -- callers
that already own a run identity (gate_runner's RunContext) can supply
a deterministic CHILD run_id instead of letting dual_deployment() mint
a completely unrelated random one, so every ephemeral resource (the
evidence directory, each scenario's own Central/Node database names,
its workspace) is traceably derived from the SAME outer run_id rather
than an anonymous one that can never be correlated back to a specific
gate run or scenario -- especially important for keep-on-failure,
where the retained database names are otherwise the only clue a local
developer has for finding what a failed run left behind.
"""

import pytest

from scripts.release_gate import e2e_support
from scripts.release_gate.environment import PreflightError


def test_dual_deployment_uses_the_supplied_run_id_when_given(tmp_path):
    explicit_run_id = "rexplicit0001"
    with e2e_support.dual_deployment(tmp_path, start_node=False, run_id=explicit_run_id) as deployment:
        assert deployment.run_id == explicit_run_id
        assert deployment.gate_names.central == f"linuxlab_gate_central_{explicit_run_id}"
        assert deployment.gate_names.node == f"linuxlab_gate_node_{explicit_run_id}"
        assert deployment.tmp_dir.name == f"gate-workspace-{explicit_run_id}"


def test_dual_deployment_generates_a_run_id_when_none_given(tmp_path):
    """Regression: the default (no run_id argument) behaves exactly like
    the already-closed Tasks 7-10 behavior -- a fresh random run_id."""
    with e2e_support.dual_deployment(tmp_path, start_node=False) as deployment:
        assert deployment.run_id is not None
        assert deployment.gate_names.central == f"linuxlab_gate_central_{deployment.run_id}"


def test_dual_deployment_rejects_an_invalid_supplied_run_id(tmp_path):
    with (
        pytest.raises(PreflightError),
        e2e_support.dual_deployment(tmp_path, start_node=False, run_id="Not-A-Valid-Run-Id!"),
    ):
        pytest.fail("must never reach the yielded block")
