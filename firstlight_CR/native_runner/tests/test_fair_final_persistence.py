from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import numpy as np
import pytest
import torch

from native_runner.tests.test_fair_final_learning import fixture_source
from native_runner.tests.test_training_v4_model import _batch, _catalog
from native_runner.training.fair_final import storage
from native_runner.training.fair_final.checkpoint import (BINDINGS, capture_rng, export_candidate,
    resume_checkpoint, save_checkpoint)
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.contracts import (AZJointEpisode, EPISODE_SCHEMA, IDENTITY_FIELDS,
    IMPLEMENTATION, STREAMS)
from native_runner.training.fair_final.learning import supervised_loss
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.replay import FrozenReplayCursor
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.storage import EpisodeStore
from native_runner.training.fair_search.contracts import pack, semantic_digest, unpack
from native_runner.training.v4.tensors import concatenate_tensor_records


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def fixture_dataset(root, *, split="train"):
    cards = list(_catalog().raw_card_ids)
    identity = {key: sha(key) for key in IDENTITY_FIELDS}
    manifest = dict(schema="FairFinalDataset/v1", implementation=IMPLEMENTATION,
        namespace_sha=sha("fixture-namespace"), identity=identity, card_ids=cards,
        matches={sha("match"): dict(pair_seed_id=sha("pair"), split=split)})
    return EpisodeStore(root, manifest=manifest)


def fixture_episode(store, *, modes=("FULL", "FAST"), attempt=1):
    cards = store.manifest["card_ids"]
    plays = torch.zeros(8, 576, 31, dtype=torch.bool)
    plays[0, 0, 1] = True
    legality = LegalPrefixes(cards, plays)
    frame = _batch(1)
    codec = ActionCodec(cards)
    act_id = codec.encode(FullAction(cards[0], 0, 1))
    rows = []
    for owner, mode in enumerate(modes):
        info = DecisionInformation(owner, 100, frame, legality)
        budget = 128 if mode == "FULL" else 16
        candidates = [dict(action_id=0, proposal="WAIT", original_joint_logp=float(np.log(.4)),
            prior_projected=.4, prior_search=.4, prior_card=.4, visits=budget // 2, q=0.),
            dict(action_id=act_id, proposal="CARD_ELITE", original_joint_logp=float(np.log(.6)),
                prior_projected=.6, prior_search=.6, prior_card=.6, visits=budget // 2, q=0.)]
        identity = store.manifest["identity"]
        search = dict(schema="SearchResult/v1", root_fair_state_sha=info.sha256, root_player=owner,
            value_perspective=owner, world_sampler_sha=identity["root_belief_prior_sha"],
            world_commitment=sha("audit-world"), search_protocol_sha=identity["search_protocol_sha"],
            opponent_controller_sha=identity["controller_red_sha" if owner == 0 else "controller_blue_sha"],
            valid_simulations=budget, invalid_attempts=0, unfinished_edge_backups=0,
            candidates=candidates, mode_mass=[.4, .6], noise_fraction=0.)
        rows.append(dict(row_id=owner, tick=100, owner=owner, frame=pack(frame), fair_sha=info.sha256,
            legality=dict(card_ids=cards, plays=plays.clone(), wait=True), pending_status="none",
            behavior_action_id=0, controller_sha=identity["controller_blue_sha" if owner == 0 else "controller_red_sha"],
            search_mode=mode, search_result=search if mode != "NONE" else None,
            policy_target=[[0, 64], [act_id, 64]] if mode == "FULL" else None,
            wdl="WIN" if owner == 0 else "LOSS",
            outcome=dict(reservation=owner, owner=owner, tick=102, reason="terminal_expired")))
    return AZJointEpisode(dict(schema=EPISODE_SCHEMA, implementation=IMPLEMENTATION, status="COMPLETE",
        match_id=sha("match"), pair_seed_id=sha("pair"), gameplay_end_tick=102, attempt_id=attempt, split=store.manifest["matches"][sha("match")]["split"],
        dataset_manifest_sha=store.dataset_sha, identity=deepcopy(store.manifest["identity"]),
        rng_stream_ids={key: sha(key + "-stream") for key in STREAMS}, card_ids=cards,
        result=dict(winner=0, terminal_tick=102, labels=["WIN", "LOSS"],
            native_evidence=dict(finalized=True, worldResult=0, worldResultRaw=0, winner=0, crownsRaw=[1, 0])),
        decisions=rows))


def ready_store(root):
    store = fixture_dataset(root)
    episode = fixture_episode(store)
    store.publish(store.stage(episode))
    return store


@pytest.mark.parametrize("modes", [("FULL", "FULL"), ("FAST", "FAST"), ("NONE", "NONE"), ("FULL", "FAST")])
def test_independent_full_fast_none_roundtrip(tmp_path, modes):
    store = fixture_dataset(tmp_path)
    episode = fixture_episode(store, modes=modes)
    staged = store.stage(episode)
    assert not (tmp_path / "completed").exists()
    assert store.publish(staged)["status"] == "published"
    restored, receipt = store.load(episode.match_id)
    assert restored.sha256 == episode.sha256 and receipt["rows"] == 2
    assert store.publish(staged)["status"] == "duplicate"


@pytest.mark.parametrize("change", ["legacy", "private_seed", "pending", "wdl", "truncated",
    "zero_delay", "frame", "attempt", "visits", "fast_policy", "perspective",
    "unfinished", "duplicate", "prior", "illegal", "missing_boundary", "unknown_outcome"])
def test_contract_rejects_corruption_and_legacy_inputs(tmp_path, change):
    store = fixture_dataset(tmp_path)
    value = fixture_episode(store).to_payload()
    row = value["decisions"][0]
    if change == "legacy": value["schema"] = "az-joint-episode.v1"
    if change == "private_seed": value["engine_seed"] = 123
    if change == "pending": row["pending_status"] = "queued"
    if change == "wdl": row["wdl"] = "LOSS"
    if change == "truncated": value["result"]["native_evidence"]["finalized"] = False
    if change == "zero_delay": row["legality"]["plays"][0, 0, 0] = True
    if change == "frame": row["fair_sha"] = sha("wrong-frame")
    if change == "attempt": value["attempt_id"] = 4
    if change == "visits": row["search_result"]["candidates"][0]["visits"] -= 1
    if change == "fast_policy": value["decisions"][1]["policy_target"] = [[0, 128]]
    if change == "perspective": row["search_result"]["value_perspective"] = 1
    if change == "unfinished": row["search_result"]["unfinished_edge_backups"] = 1
    if change == "duplicate": row["search_result"]["candidates"][1]["action_id"] = 0
    if change == "prior": row["search_result"]["candidates"][0]["prior_projected"] = .9
    if change == "illegal": row["behavior_action_id"] = 1
    if change == "missing_boundary": row["outcome"]["reason"] = "preempted"
    if change == "unknown_outcome": row["outcome"]["reason"] = "rejected"
    with pytest.raises((ValueError, TypeError)):
        AZJointEpisode(value)


def test_registered_splits_reject_cross_split_and_retry_counts_once(tmp_path):
    store = fixture_dataset(tmp_path)
    episode = fixture_episode(store)
    store.publish(store.stage(episode))
    retry = fixture_episode(store, attempt=2)
    assert store.publish(store.stage(retry))["status"] == "duplicate"
    assert len(list((tmp_path / "completed").iterdir())) == 1
    changed = episode.to_payload()
    changed["split"] = "test"
    with pytest.raises(ValueError, match="split"):
        store.stage(AZJointEpisode(changed))
    manifest = deepcopy(store.manifest)
    manifest["matches"][sha("other")] = dict(pair_seed_id=sha("pair"), split="validation")
    with pytest.raises(ValueError, match="leak"):
        EpisodeStore(tmp_path / "leak", manifest=manifest)


def test_publication_failure_and_new_process_retry(tmp_path, monkeypatch):
    store = fixture_dataset(tmp_path)
    episode = fixture_episode(store)
    staged = store.stage(episode)
    original = storage.durable_write

    def interrupt(path, data):
        if Path(path).parent.name == "completed":
            raise OSError("injected interruption")
        return original(path, data)

    monkeypatch.setattr(storage, "durable_write", interrupt)
    with pytest.raises(OSError):
        store.publish(staged)
    assert not (tmp_path / "completed").exists()
    command = [sys.executable, "-m", __name__, "publish", str(tmp_path), staged]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["status"] == "published"
    assert store.load(episode.match_id)[0].sha256 == episode.sha256


def test_disk_tampering_and_legacy_contract_are_rejected(tmp_path):
    store = ready_store(tmp_path)
    episode, receipt = store.load(sha("match"))
    with pytest.raises(TypeError):
        store.stage({"schema": "FairFinalAcceptanceEpisode/v1"})
    path = tmp_path / "staged" / (episode.sha256 + ".pt.gz")
    path.write_bytes(path.read_bytes()[:-11])
    with pytest.raises((ValueError, EOFError)):
        store.load(episode.match_id)


def test_replay_resumes_exact_rows_and_budget_and_rejects_validation(tmp_path):
    store = ready_store(tmp_path / "train")
    sampler = FrozenReplayCursor(store, [sha("match")], seed=123, sample_budget=12)
    sampler.sample(3)
    state = sampler.state_dict()
    expected = sampler.sample(5)
    restored = FrozenReplayCursor(store, [sha("match")], seed=999, sample_budget=12)
    restored.load_state_dict(state)
    assert restored.sample(5) == expected
    assert restored.state_dict() == sampler.state_dict()
    bad = deepcopy(state)
    bad["consumed"] += 1
    with pytest.raises(ValueError):
        restored.load_state_dict(bad)
    with pytest.raises(ValueError, match="budget"):
        restored.sample(5)
    validation = fixture_dataset(tmp_path / "validation", split="validation")
    validation.publish(validation.stage(fixture_episode(validation)))
    with pytest.raises(ValueError, match="never"):
        FrozenReplayCursor(validation, [sha("match")], seed=0, sample_budget=8)


def test_replay_equal_episode_quota_does_not_favor_long_games(tmp_path):
    original = fixture_dataset(tmp_path / "manifest-source")
    manifest = deepcopy(original.manifest)
    manifest["matches"][sha("long-match")] = dict(pair_seed_id=sha("long-pair"), split="train")
    store = EpisodeStore(tmp_path / "replay", manifest=manifest)
    short = fixture_episode(store, modes=("NONE", "NONE"))
    store.publish(store.stage(short))
    longer = short.to_payload()
    longer["match_id"], longer["pair_seed_id"] = sha("long-match"), sha("long-pair")
    next_rows = deepcopy(longer["decisions"])
    for row in longer["decisions"]:
        row["outcome"].update(tick=101, reason="preempted")
    for row in next_rows:
        row["tick"], row["row_id"] = 101, row["row_id"] + 2
        row["outcome"]["reservation"] = row["row_id"]
        legality = LegalPrefixes(row["legality"]["card_ids"], row["legality"]["plays"])
        row["fair_sha"] = DecisionInformation(row["owner"], 101, unpack(row["frame"]), legality).sha256
    longer["decisions"].extend(next_rows)
    store.publish(store.stage(AZJointEpisode(longer)))
    sampler = FrozenReplayCursor(store, list(manifest["matches"]), seed=27, sample_budget=20)
    sampler.sample(20)
    assert {key: sum(values) for key, values in sampler.counts.items()} == {sha("match"): 10, sha("long-match"): 10}
    assert all(max(counts) - min(counts) <= 1 for counts in sampler.counts.values())
    sampler.load_state_dict(sampler.state_dict())


def fixture_model():
    source, bundle = fixture_source()
    model = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config).train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
    return model, optimizer, bundle


def advance_model(model, optimizer, sampler):
    selected = sampler.sample(2)
    rows = [sampler.store.load(match_id)[0].payload["decisions"][index] for match_id, index in selected]
    frame = concatenate_tensor_records(tuple(unpack(row["frame"]) for row in rows))
    legalities = [LegalPrefixes(row["legality"]["card_ids"], row["legality"]["plays"]) for row in rows]
    codec = ActionCodec(sampler.store.manifest["card_ids"])
    targets = [None if row["policy_target"] is None else
        [(codec.decode(action_id), count) for action_id, count in row["policy_target"]] for row in rows]
    policies, value = model(frame, legalities)
    labels = torch.tensor([["WIN", "DRAW", "LOSS"].index(row["wdl"]) for row in rows])
    loss = supervised_loss(policies, value, targets, labels)["loss"]
    draw = random.random() + float(np.random.random()) + float(torch.rand(()))
    (loss * (1. + draw * .01)).backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    return dict(rows=selected, loss=float(loss.detach()), random=draw)


def continuation_summary(model, optimizer, sampler):
    trace = [advance_model(model, optimizer, sampler) for step in range(2)]
    return dict(network_sha=semantic_digest(model.state_dict()), optimizer_sha=semantic_digest(optimizer.state_dict()),
        replay_sha=semantic_digest(sampler.state_dict()), rng_sha=semantic_digest(capture_rng()), trace=trace)


def fixture_bindings():
    return {**{key: sha(key) for key in BINDINGS}, "fair_observation_sha": sha("observation_contract_sha")}


def test_checkpoint_new_process_replays_identical_updates_and_candidate(tmp_path):
    torch.set_num_threads(1)
    random.seed(17); np.random.seed(17); torch.manual_seed(17)
    store = ready_store(tmp_path / "store")
    sampler = FrozenReplayCursor(store, [sha("match")], seed=9, sample_budget=12)
    model, optimizer, bundle = fixture_model()
    advance_model(model, optimizer, sampler)
    bindings = fixture_bindings()
    checkpoint = tmp_path / "learner.pt"
    identity = save_checkpoint(checkpoint, model, optimizer, sampler, bindings=bindings, optimizer_steps=1, batch_size=2)
    expected = continuation_summary(model, optimizer, sampler)
    child = subprocess.run([sys.executable, "-m", __name__, "resume", str(tmp_path), identity["file_sha"]],
        capture_output=True, text=True, check=True)
    actual = json.loads(child.stdout)
    assert json.loads(json.dumps(expected)) == actual
    candidate_identity = export_candidate(checkpoint, tmp_path / "candidate.pt", expected_sha=identity["file_sha"], bindings=bindings)
    candidate = torch.load(tmp_path / "candidate.pt", weights_only=True)
    assert candidate_identity["network_sha"] == identity["network_sha"]
    assert candidate["role"] == "CANDIDATE" and "optimizer" not in candidate and "replay" not in candidate
    restored, _, _, _ = resume_checkpoint(checkpoint, expected_sha=identity["file_sha"], bindings=bindings, store=store, bundle=bundle)
    exported, _, _ = fixture_model()
    exported.load_state_dict(candidate["network"], strict=True)
    restored.eval(); exported.eval()
    masks = torch.zeros(8, 576, 31, dtype=torch.bool)
    masks[0, 0, 1] = True
    legal = LegalPrefixes(store.manifest["card_ids"], masks)
    with torch.no_grad():
        left_policy, left_value = restored(_batch(1), [legal])
        right_policy, right_value = exported(_batch(1), [legal])
        torch.testing.assert_close(left_value, right_value, rtol=0, atol=0)
        for action in (FullAction(), FullAction(legal.card_ids[0], 0, 1)):
            torch.testing.assert_close(left_policy[0].log_prob(action), right_policy[0].log_prob(action), rtol=0, atol=0)
    with pytest.raises(FileExistsError):
        export_candidate(checkpoint, tmp_path / "candidate.pt", expected_sha=identity["file_sha"], bindings=bindings)
    before = semantic_digest(capture_rng())
    bad_bindings = dict(bindings, fair_observation_sha=sha("changed"))
    with pytest.raises(ValueError, match="bindings"):
        resume_checkpoint(checkpoint, expected_sha=identity["file_sha"], bindings=bad_bindings, store=store, bundle=bundle)
    assert semantic_digest(capture_rng()) == before


@pytest.fixture
def saved_checkpoint(tmp_path):
    torch.set_num_threads(1)
    store = ready_store(tmp_path / "store")
    sampler = FrozenReplayCursor(store, [sha("match")], seed=19, sample_budget=8)
    model, optimizer, bundle = fixture_model()
    for parameter in model.encoder.parameters():
        parameter.requires_grad_(False)
    model.encoder.eval()
    advance_model(model, optimizer, sampler)
    path = tmp_path / "learner.pt"
    identity = save_checkpoint(path, model, optimizer, sampler, bindings=fixture_bindings(), optimizer_steps=1, batch_size=2)
    return path, identity, model, optimizer, sampler, store, bundle


def test_restores_frozen_encoder_modes_and_moments(saved_checkpoint):
    path, identity, model, optimizer, sampler, store, bundle = saved_checkpoint
    restored, resumed, cursor, payload = resume_checkpoint(path, expected_sha=identity["file_sha"],
        bindings=fixture_bindings(), store=store, bundle=bundle)
    assert restored.training and not restored.encoder.training
    assert all(not parameter.requires_grad for parameter in restored.encoder.parameters())
    assert semantic_digest(optimizer.state_dict()) == semantic_digest(resumed.state_dict())
    assert semantic_digest(sampler.state_dict()) == semantic_digest(cursor.state_dict())


@pytest.mark.parametrize("change", ["parameter_order", "moment_shape", "reset_optimizer",
    "budget", "replay_hash", "network_dtype", "rng_topology", "schema"])
def test_resume_rejects_bad_checkpoints_without_changing_rng(saved_checkpoint, tmp_path, change):
    path, identity, model, optimizer, sampler, store, bundle = saved_checkpoint
    envelope = torch.load(path, weights_only=True)
    payload = envelope["payload"]
    if change == "parameter_order": payload["optimizer_parameter_names"].reverse()
    if change == "moment_shape": next(iter(payload["optimizer"]["state"].values()))["exp_avg"] = torch.zeros(1)
    if change == "reset_optimizer": payload["optimizer"]["state"] = {}
    if change == "budget": payload["trainer"]["consumed"] += 1
    if change == "replay_hash": payload["replay"]["entries"][sha("match")]["sha"] = sha("changed")
    if change == "network_dtype":
        name = next(name for name, tensor in payload["network"].items() if tensor.is_floating_point())
        payload["network"][name] = payload["network"][name].to(torch.float16)
    if change == "rng_topology": payload["rng"]["cuda"] = [torch.get_rng_state()]
    if change == "schema": payload["schema"] = "az-joint-checkpoint.v1"
    for field in ("network", "optimizer", "replay", "rng"):
        payload[field + "_sha"] = semantic_digest(payload[field])
    envelope["sha256"] = semantic_digest(payload)
    damaged = tmp_path / (change + ".pt")
    torch.save(envelope, damaged)
    before = semantic_digest(capture_rng())
    with pytest.raises(ValueError):
        resume_checkpoint(damaged, expected_sha=hashlib.sha256(damaged.read_bytes()).hexdigest(),
            bindings=fixture_bindings(), store=store, bundle=bundle)
    assert semantic_digest(capture_rng()) == before


def test_save_refuses_partial_gradients_or_unaccounted_consumption(saved_checkpoint, tmp_path):
    path, identity, model, optimizer, sampler, store, bundle = saved_checkpoint
    parameter = next(model.trunk.parameters())
    parameter.grad = torch.ones_like(parameter)
    with pytest.raises(ValueError, match="boundaries"):
        save_checkpoint(tmp_path / "partial.pt", model, optimizer, sampler, bindings=fixture_bindings(), optimizer_steps=1, batch_size=2)
    parameter.grad = None
    sampler.sample(1)
    with pytest.raises(ValueError, match="complete optimizer"):
        save_checkpoint(tmp_path / "partial.pt", model, optimizer, sampler, bindings=fixture_bindings(), optimizer_steps=1, batch_size=2)
    assert not (tmp_path / "partial.pt").exists()


def test_process_death_after_staging_recovers_exactly_once(tmp_path):
    child = subprocess.run([sys.executable, "-m", __name__, "crash", str(tmp_path), "unused"],
        capture_output=True, text=True)
    assert child.returncode == 73
    store = EpisodeStore(tmp_path, manifest=json.loads((tmp_path / "dataset.json").read_text()))
    staged = list((tmp_path / "staged").glob("*.pt.gz"))
    assert len(staged) == 1 and not (tmp_path / "completed").exists()
    identity = staged[0].name.removesuffix(".pt.gz")
    children = [subprocess.Popen([sys.executable, "-m", __name__, "publish", str(tmp_path), identity],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for ordinal in range(2)]
    statuses = []
    for process in children:
        output, error = process.communicate(timeout=60)
        assert process.returncode == 0, error
        statuses.append(json.loads(output)["status"])
    assert sorted(statuses) == ["duplicate", "published"]
    assert len(list((tmp_path / "completed").iterdir())) == 1
    assert store.load(sha("match"))[0].sha256 == identity


if __name__ == "__main__":
    torch.set_num_threads(1)
    mode, root, identity = sys.argv[1:]
    root = Path(root)
    if mode == "publish":
        store = EpisodeStore(root, manifest=json.loads((root / "dataset.json").read_text()))
        print(json.dumps(store.publish(identity)))
    elif mode == "crash":
        store = fixture_dataset(root)
        store.stage(fixture_episode(store))
        os._exit(73)
    elif mode == "resume":
        store = EpisodeStore(root / "store", manifest=json.loads((root / "store/dataset.json").read_text()))
        _, bundle = fixture_source()
        model, optimizer, sampler, payload = resume_checkpoint(root / "learner.pt", expected_sha=identity,
            bindings=fixture_bindings(), store=store, bundle=bundle)
        print(json.dumps(continuation_summary(model, optimizer, sampler)))
    else:
        raise ValueError("unknown test worker")
