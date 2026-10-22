from copy import deepcopy
from dataclasses import asdict

import pytest
import torch

from native_runner.tests.test_fair_final_search import Belief
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import pack
from tools.probe_fair_final_search_episode import SearchTrace, outcome_signature, validate_group


@pytest.fixture
def group():
    plays = torch.zeros(8, 2, 31, dtype=torch.bool)
    plays[0, 0, 1:3] = True
    legality = LegalPrefixes(HOG_DECK, plays)
    information = DecisionInformation(0, 100, {"visible": True}, legality)

    class Inference:
        checkpoint_sha = "a" * 64
        controller_sha = "b" * 64

        def predict(self, info):
            return JointPolicy(lambda prefix: torch.zeros_like(info.legality.mask(prefix),
                dtype=torch.float32), info.legality), torch.tensor([[1., 0., -1.]])

    inference, belief, limits = Inference(), Belief(incomplete_after=2), SearchLimits()
    identity = dict(search_protocol_sha=limits.sha256,root_belief_prior_sha=belief.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha,checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha,controller_red_sha=inference.controller_sha)
    outcome = CompletedBoundarySearch(belief,inference,inference,limits=limits).run(information,
        seed=42,behavior_seed=19,codec=ActionCodec(HOG_DECK),identity=identity)
    return dict(tick=100, information={"0":dict(frame=pack(information.frame),
        card_ids=legality.card_ids,plays=pack(legality.plays),fair_sha=information.sha256,history_sha="f"*64)},
        outcomes={"0":dict(signature=outcome_signature(outcome),audit=outcome.audit)},
        identity=identity,pending=[1],actual_unchanged=True)


def test_completed128_search_trace_roundtrips_and_rebuilds_actor_rows(tmp_path,group):
    trace = SearchTrace(tmp_path)
    trace.write(group)
    recovered = SearchTrace(tmp_path)
    assert recovered.load(100)["outcomes"] == group["outcomes"]
    from native_runner.training.fair_search.contracts import unpack
    encoded = group["information"]["0"]
    information = DecisionInformation(0,100,unpack(encoded["frame"]),
        LegalPrefixes(encoded["card_ids"],unpack(encoded["plays"])))
    action = FullAction(**group["outcomes"]["0"]["signature"]["selected"])
    recovered.confirm({0:information},{0:action})
    assert recovered.committed == [100]
    with pytest.raises(ValueError,match="duplicate"):
        recovered.confirm({0:information},{0:action})
    with pytest.raises(FileExistsError):
        trace.write(group)


@pytest.mark.parametrize("damage", ["budget","target","frame","unfinished","actual","pending",
    "owner","missing","behavior"])
def test_search_trace_rejects_invalid_or_partial_boundary(group,damage):
    value = deepcopy(group)
    signature = value["outcomes"]["0"]["signature"]
    if damage == "budget":
        signature["result"]["valid_simulations"] = 127
    elif damage == "target":
        signature["target"][0][1] += 1
    elif damage == "frame":
        value["information"]["0"]["fair_sha"] = "e"*64
    elif damage == "unfinished":
        signature["result"]["unfinished_edge_backups"] = 1
    elif damage == "actual":
        value["actual_unchanged"] = False
    elif damage == "pending":
        value["pending"] = [0]
    elif damage == "owner":
        signature["result"]["value_perspective"] = 1
    elif damage == "missing":
        value["outcomes"] = {}
    else:
        signature["selected"] = asdict(FullAction(HOG_DECK[-1],1,30))
    with pytest.raises(ValueError):
        validate_group(value)


def test_corrupted_durable_search_evidence_is_rejected(tmp_path,group):
    trace = SearchTrace(tmp_path)
    trace.write(group)
    trace.path(100).write_bytes(trace.path(100).read_bytes()[:20])
    with pytest.raises((EOFError,OSError)):
        trace.load(100)
