import pytest

from audit_boundaries import FinalDecisionRuntime, audit, cases, run_case


def test_complete_matrix_and_reproducibility():
    first = audit()
    assert first == audit()
    assert first["counts"]["cases"] == len(list(cases()))
    assert first["counts"]["all_success_cases"] == 961
    assert first["counts"]["single_ready_cases"] > 0
    assert first["counts"]["cases"] == sum(first["counts"][name]
        for name in ("synchronous_cases", "single_ready_cases"))
    assert first["legal_mask_or_native_executor_tested"] is False
    assert first["native_rejection_reachability_proven"] is False
    assert first["native_rejection_unreachability_proven"] is False
    assert first["native_fallback_verified"] is False
    assert first["training_admissible"] is False
    assert first["gates_closed"] is False


@pytest.mark.parametrize("owner", [0, 1])
def test_private_rejection_leaves_other_player_pending(owner):
    choices = (1, 0) if owner == 0 else (0, 1)
    row = run_case(choices, {owner: "rejected"})
    assert row["ready"] == [owner]
    assert row["pending"] == {1 - owner: 160}


@pytest.mark.parametrize("outcomes", [
    {0: "executed", 1: "executed"}, {0: "executed", 1: "rejected"},
    {0: "rejected", 1: "executed"}, {0: "rejected", 1: "rejected"},
])
def test_equal_deadlines_settle_both_before_observing(outcomes):
    row = run_case((30, 30), outcomes)
    assert row["ready"] == [0, 1]
    assert row["pending"] == {}
    assert row["reasons"] == outcomes


def test_changed_protocol_rejected():
    class DifferentProtocol(FinalDecisionRuntime):
        wake_protocol = "unregistered-private-wake"

    with pytest.raises(ValueError, match="protocol"):
        audit(runtime_class=DifferentProtocol)


def test_premature_observation_detected():
    class EarlyWake(FinalDecisionRuntime):
        def advance(self):
            result = super().advance()
            self.clock.ready.add(0)
            return result

    with pytest.raises(ValueError, match="before earliest"):
        run_case((1, 0), {0: "executed"}, runtime_class=EarlyWake)


def test_lost_preemption_detected():
    class LostPreemption(FinalDecisionRuntime):
        def advance(self):
            plans = dict(self.clock.pending)
            result = super().advance()
            if self.clock.ready and 1 not in self.clock.pending:
                self.clock.pending[1] = plans[1]
            return result

    with pytest.raises(ValueError, match="formula"):
        run_case((1, 0), {0: "executed"}, runtime_class=LostPreemption)


def test_incomplete_due_results_rejected():
    with pytest.raises(ValueError, match="outcome assignment"):
        run_case((1, 1), {0: "executed"})
