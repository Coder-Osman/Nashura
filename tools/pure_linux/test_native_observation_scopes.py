import pytest

from native_observation_scopes import ObservationScopes


def test_scopes_keep_threads_independent():
    scopes = ObservationScopes()
    scopes.enter("batch", (1, 2, 3))
    scopes.enter("batch", (1, 2, 4))
    scopes.leave("batch", (1, 2, 3))
    assert not scopes.contains("batch", (1, 2, 3))
    assert scopes.contains("batch", (1, 2, 4))
    assert scopes.any("batch")
    scopes.leave("batch", (1, 2, 4))
    assert not scopes.any("batch")


def test_invalid_scope_transitions_are_explicit():
    scopes = ObservationScopes()
    scopes.enter("batch", (1, 2, 3))
    with pytest.raises(ValueError, match="nested"):
        scopes.enter("batch", (1, 2, 3))
    with pytest.raises(ValueError, match="missing"):
        scopes.leave("batch", (1, 2, 4))
