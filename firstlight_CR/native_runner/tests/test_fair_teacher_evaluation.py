from types import SimpleNamespace

import pytest

from native_runner.training.fair_search.evaluate_teacher import sample_visit_action, select_frozen_actions


@pytest.mark.parametrize("teacher_owner", [0, 1])
def test_teacher_selection_uses_frozen_views_and_evaluation_mode(teacher_owner):
    views = {owner: SimpleNamespace(owner=owner, sha256=str(owner) * 64,
                                    legal=lambda action: action == "wait")
             for owner in (0, 1)}
    calls = []

    def search(information, **kwargs):
        assert information is views[teacher_owner]
        assert kwargs["training"] is False
        assert kwargs["deadline"] == 1234
        calls.append(("search", kwargs["seed"]))
        return SimpleNamespace(visits={"wait": 32}, selected="wait")

    def direct(information):
        assert information is views[1 - teacher_owner]
        calls.append(("direct", information.owner))
        return "wait"

    selected, result = select_frozen_actions(
        views, teacher_owner, SimpleNamespace(run=search), direct,
        decision_index=10, deadline=1234)
    assert selected == {0: "wait", 1: "wait"}
    assert result.visits == {"wait": 32}
    assert len(calls) == 2
    repeated = []

    def repeat(information, **kwargs):
        repeated.append(kwargs["seed"])
        return result

    select_frozen_actions(views, teacher_owner, SimpleNamespace(run=repeat), direct,
                          decision_index=10, deadline=1234)
    assert repeated == [seed for kind, seed in calls if kind == "search"]


@pytest.mark.parametrize("visits", [0, 31, 33])
def test_incomplete_teacher_budget_is_rejected(visits):
    information = SimpleNamespace(sha256="a" * 64, legal=lambda action: True)
    search = SimpleNamespace(run=lambda *args, **kwargs:
                             SimpleNamespace(visits={"wait": visits}, selected="wait"))
    with pytest.raises(ValueError, match="32 simulations"):
        select_frozen_actions({0: information}, 0, search, None, decision_index=0, deadline=None)


def test_illegal_direct_action_is_rejected():
    information = SimpleNamespace(legal=lambda action: False)
    with pytest.raises(ValueError, match="illegal action"):
        select_frozen_actions({1: information}, 0, None, lambda info: "play:7:0:0",
                              decision_index=0, deadline=None)


def test_visit_sampling_matches_full_joint_target_and_never_selects_zero_visits():
    visits = {"wait": 8, "play:0:1:2": 24, "play:0:2:1": 0}
    actions = [sample_visit_action(visits, seed=seed) for seed in range(4096)]
    assert set(actions) == {"wait", "play:0:1:2"}
    assert abs(actions.count("wait") / len(actions) - .25) < .025
    assert sample_visit_action(dict(reversed(list(visits.items()))), seed=17) == actions[17]


@pytest.mark.parametrize("visits", [{}, {"wait": 31}, {"wait": 33, "play": -1},
                                    {"wait": 32.0}, {"wait": True, "play": 31}])
def test_invalid_sampling_target_is_rejected(visits):
    with pytest.raises(ValueError, match="integer visits"):
        sample_visit_action(visits, seed=0)


@pytest.mark.parametrize("root_noise", [False, True])
@pytest.mark.parametrize("tick", [0, 1200, 4000])
def test_sampling_uses_independent_seed_and_untempered_visits(root_noise, tick):
    information = SimpleNamespace(sha256="a" * 64, tick=tick, legal=lambda action: action in ("wait", "play"))
    visits = {"wait": 24, "play": 8}
    calls = []

    def search(view, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(visits=visits, selected="ignored-search-selection")

    for seed in (0, 1, 2):
        selected, result = select_frozen_actions(
            {0: information}, 0, SimpleNamespace(run=search), None,
            decision_index=7, deadline=1234, teacher_selection="sample-visits",
            root_noise=root_noise, sampling_seed=seed)
        assert selected[0] == sample_visit_action(visits, seed=seed)
    assert all(call["training"] is root_noise for call in calls)
    assert len({call["seed"] for call in calls}) == 1


def test_sampling_requires_explicit_independent_action_seed():
    with pytest.raises(ValueError, match="independent action seed"):
        select_frozen_actions({}, 0, None, None, decision_index=0, deadline=None,
                              teacher_selection="sample-visits")
