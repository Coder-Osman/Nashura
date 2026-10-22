"""Supervised entrypoints cannot silently consume native-cell legacy labels."""
from types import SimpleNamespace

import pytest

from tools.train_event_pretrain import _shard_batch
from tools.eval_event_pretrain import shard_batch


@pytest.mark.parametrize("batcher", [_shard_batch, shard_batch])
@pytest.mark.parametrize("contract", [None, {"version": "event-observation-cache-contract.v1"}])
def test_training_and_evaluation_reject_unverified_label_contract(batcher, contract):
    shard = SimpleNamespace(manifest={"contract": contract})
    with pytest.raises(ValueError):
        batcher([shard])
