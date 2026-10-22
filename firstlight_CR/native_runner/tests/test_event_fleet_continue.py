from pathlib import Path

import pytest

from tools.event_rl_continue import cache_candidates


def test_cache_retains_selected_checkpoints():
    paths=[Path(character*64+'.pt') for character in 'abc']
    assert cache_candidates(paths,[paths[1].name,paths[2].name])==[paths[0]]


@pytest.mark.parametrize('name',['initial.pt','notes.txt','G'*64+'.pt','a'*63+'.pt'])
def test_unexpected_file_prevents_cache_removal(name):
    with pytest.raises(ValueError,match='non-content-addressed'):
        cache_candidates([Path(name)],[])
