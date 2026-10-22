"""Deterministic tree-search and native-state contract tests."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import json
import unittest

import torch

from distillation import TeacherRecord, distillation_loss, encode_model_inputs, split_episodes
from native_adapter import BattleEnvSidecar, NativeSnapshotSearchAdapter
from policy_signature import tensor_record_sha256
from native_runner.tests.test_training_v4_model import _batch
from tree_search import (
    OpponentResponse, PuctSearch, SearchState, SearchView, Transition,
    aggregate_opponent_values, frozen_opponent_target, q_target, state_digest,
)


class FixtureAdapter:
    """A deterministic, complete-state adapter for PUCT unit tests."""

    def __init__(self, graph, actors):
        self.graph, self.actors = graph, actors
        self.current = "root"
        self.pending = None
        self.advance_calls = []
        self.handle = 0

    def state(self):
        self.handle += 1
        actions = tuple(action for source, action in self.graph if source == self.current)
        return SearchState(
            native_snapshot={"handle": self.handle, "state": self.current},
            observation={"state": self.current, "truncated": False},
            actor=self.actors[self.current], legal_actions=actions,
            external_queue=(), reward_ledger={"r": self.current},
            event_cursor={"tick": self.current}, policy_state={"hidden": self.current},
            terminal=not bool(actions),
        )

    def save_search_state(self):
        return self.state()

    def restore_search_state(self, state):
        self.current = state.native_snapshot["state"]
        self.pending = None

    def apply_action(self, action):
        if (self.current, action) not in self.graph:
            raise ValueError("illegal fixture action")
        self.pending = action

    def advance_until_decision(self, max_decision_points, max_native_seconds):
        self.advance_calls.append((max_decision_points, max_native_seconds))
        next_state, reward = self.graph[self.current, self.pending]
        self.current, self.pending = next_state, None
        return Transition(self.state(), reward, 1, 0.25)

    def fingerprint(self):
        return self.current


class ProfiledFixtureAdapter(FixtureAdapter):
    profile_enabled = True

    def __init__(self, graph, actors):
        super().__init__(graph, actors)
        self.counters = {}

    def _count_profile(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount

    def _max_profile(self, name, value):
        self.counters[name] = max(self.counters.get(name, 0), value)

    def _add_profile(self, name, started):
        pass


class SearchTests(unittest.TestCase):
    def test_state_round_trip_and_digest_ignore_operational_handle(self):
        adapter = FixtureAdapter({("root", "wait"): ("end", 0)}, {"root": 0, "end": 1})
        first, second = adapter.state(), adapter.state()
        self.assertEqual(SearchState.from_dict(first.to_dict()), first)
        self.assertEqual(state_digest(first), state_digest(second))

    def test_search_view_digest_binds_native_execution_digest(self):
        def view(digest):
            return SearchView(
                observation={"tick": 4, "truncated": False}, actor=0,
                legal_actions=("wait",), external_queue=(), reward_ledger={},
                event_cursor={}, policy_state={}, execution_digest=digest,
            )
        self.assertNotEqual(state_digest(view("a" * 64)), state_digest(view("b" * 64)))

    def test_state_digest_hashes_tensor_bytes_without_mutating_input(self):
        adapter = FixtureAdapter({("root", "wait"): ("end", 0)}, {"root": 0, "end": 1})
        state = adapter.state()
        state = replace(state, observation={"truncated": False, "tensor": torch.tensor([1., 2.])})
        original = state.observation["tensor"].clone()
        initial_digest = state_digest(state)
        state.observation["tensor"][0] = 3.
        self.assertNotEqual(initial_digest, state_digest(state))
        self.assertTrue(torch.equal(original, torch.tensor([1., 2.])))

    def test_state_digest_preserves_group_membership_across_native_event_epochs(self):
        def branch(epoch, sequence, handle, groups):
            return SearchState(
                native_snapshot={"handle": handle, "stateEpoch": epoch, "state_sha256": "native"},
                observation={"truncated": False, "fair": {"0": {"entities": [
                    {"entity_id": entity, "causal_group": {"kind": "deployment",
                                                      "handle": f"deploy:6:{epoch}:{group}"}}
                    for entity, group in groups]}}},
                actor=0, legal_actions=("wait",), external_queue=(), reward_ledger={"reward": 1},
                event_cursor={"seen": [f"{epoch}:{sequence}"], "combat": sequence + 4,
                              "public_card_play": None},
                policy_state={"rollout": {"seen_events": [f"{epoch}:{sequence}"]}},
            )

        first = branch(28, 87, 22, [(1, 26), (2, 26), (3, 27)])
        replay = branch(30, 93, 24, [(1, 29), (2, 29), (3, 30)])
        changed_group = branch(30, 93, 24, [(1, 29), (2, 30), (3, 30)])
        changed_cursor = branch(30, 93, 24, [(1, 29), (2, 29), (3, 30)])
        changed_cursor = SearchState.from_dict({**changed_cursor.to_dict(), "event_cursor": {
            "seen": ["30:93"], "combat": 98, "public_card_play": None}})
        self.assertEqual(state_digest(first), state_digest(replay))
        self.assertNotEqual(state_digest(first), state_digest(changed_group))
        self.assertNotEqual(state_digest(first), state_digest(changed_cursor))
        self.assertEqual(first.event_cursor["combat"], 91)

    def test_state_digest_preserves_empty_cursor_and_multiple_event_identity(self):
        adapter = FixtureAdapter({("root", "wait"): ("end", 0)}, {"root": 0, "end": 1})
        base = adapter.state().to_dict()
        base["event_cursor"] = {"seen": [], "combat": 7, "public_card_play": None}
        changed = copy.deepcopy(base)
        changed["event_cursor"]["combat"] = 8
        self.assertEqual(state_digest(SearchState.from_dict(base)),
                         state_digest(SearchState.from_dict(changed)))

        base["event_cursor"] = {
            "seen": ["3:7", "3:9"], "combat": 11, "public_card_play": None,
        }
        replay = copy.deepcopy(base)
        replay["event_cursor"] = {
            "seen": ["5:17", "5:19"], "combat": 21, "public_card_play": None,
        }
        self.assertEqual(state_digest(SearchState.from_dict(base)),
                         state_digest(SearchState.from_dict(replay)))
        base["event_cursor"]["seen"].append("3:7")
        with self.assertRaisesRegex(ValueError, "repeats an identity"):
            state_digest(SearchState.from_dict(base))

    def test_discounted_reward_and_total_horizon(self):
        adapter = FixtureAdapter(
            {("root", "fire"): ("reply", 1.0), ("reply", "defend"): ("terminal", 2.0)},
            {"root": 0, "reply": 1, "terminal": 0},
        )
        search = PuctSearch(
            adapter, lambda _: {"fire": 1}, lambda _: {"defend": 1},
            lambda _: 0, gamma=0.5, max_decision_points=2,
        )
        root = search.run(3)
        self.assertEqual(root.visits["fire"], 3)
        self.assertAlmostEqual(root.value_sum["fire"], 5.0)
        self.assertEqual(adapter.current, "root")
        self.assertEqual([n for n, _ in adapter.advance_calls], [2, 2, 1, 2, 1])
        self.assertEqual(len(root.children), 1)

    def test_search_counters_distinguish_new_edges_from_tree_replays(self):
        adapter = ProfiledFixtureAdapter(
            {("root", "fire"): ("reply", 0.),
             ("reply", "left"): ("end-left", 0.),
             ("reply", "right"): ("end-right", 0.)},
            {"root": 0, "reply": 1, "end-left": 0, "end-right": 0},
        )
        search = PuctSearch(
            adapter, lambda _: {"fire": 1.}, lambda _: {"left": .6, "right": .4},
            lambda _: 0., seed=3,
        )
        root = search.run(3)

        counters = adapter.counters
        assert counters["simulation_count"] == 3
        assert counters["edge_traversals"] == 5
        assert counters["edge_first_expansions"] + counters["edge_replays"] == 5
        assert counters["state_key_calls"] == 5
        assert counters["state_key_new_edge_calls"] == counters["edge_first_expansions"]
        assert counters["state_key_existing_edge_calls"] == counters["edge_replays"]
        assert counters["max_search_depth"] == 2
        assert counters["sum_search_depth"] == 5
        assert root.visits["fire"] == 3

    def test_opponent_is_sampled_from_frozen_policy(self):
        adapter = FixtureAdapter(
            {("root", "fire"): ("reply", 0), ("reply", "strong"): ("lost", -1),
             ("reply", "weak"): ("won", 1)},
            {"root": 0, "reply": 1, "lost": 0, "won": 0},
        )
        search = PuctSearch(
            adapter, lambda _: {"fire": 1}, lambda _: {"strong": .8, "weak": .2},
            lambda _: 0, gamma=1, seed=17,
        )
        root = search.run(1000)
        self.assertLess(abs(root.q("fire") - (-15 / 17)), .08)
        opponent = next(iter(root.children.values()))
        self.assertGreater(opponent.visits["strong"], opponent.visits["weak"])
        self.assertEqual(adapter.current, "root")

    def test_response_aggregation_and_target(self):
        expected, robust = aggregate_opponent_values([
            OpponentResponse("strong", .8, -1), OpponentResponse("weak", .2, 1)])
        self.assertAlmostEqual(expected, -.6)
        self.assertEqual(robust, -1)
        self.assertAlmostEqual(q_target(expected, robust), -.72)

    def test_frozen_opponent_target_requires_exact_top_four(self):
        prior = {f"a{i}": 8 - i for i in range(6)}
        values = {"a0": -1., "a1": 0., "a2": .5, "a3": 1.}
        expected, robust, target = frozen_opponent_target(prior, tuple(prior), values)
        self.assertEqual(robust, -1.)
        self.assertAlmostEqual(target, .7 * expected - .3)
        with self.assertRaisesRegex(ValueError, "Top-K"):
            frozen_opponent_target(prior, tuple(prior), {"a0": -1.})
        with self.assertRaisesRegex(ValueError, "Top-K"):
            frozen_opponent_target(prior, tuple(prior), {**values, "a4": 1.})

    def test_root_reply_values_use_frozen_counterplay(self):
        adapter = FixtureAdapter(
            {("root", "fire"): ("reply", .2), ("reply", "strong"): ("lost", -1.),
             ("reply", "weak"): ("won", 1.)},
            {"root": 0, "reply": 1, "lost": 0, "won": 0},
        )
        search = PuctSearch(
            adapter, lambda _: {"fire": 1.}, lambda _: {"strong": .8, "weak": .2},
            lambda _: 0., gamma=1.,
        )
        root = search.run(32)
        expected, robust, target = search.evaluate_root_replies(root, "fire")
        self.assertAlmostEqual(expected, .2 + (16 * -1 + 1) / 17)
        self.assertAlmostEqual(robust, -.8)
        self.assertAlmostEqual(target, .7 * expected + .3 * robust)
        self.assertEqual(adapter.current, "root")

    def test_root_reply_requires_opponent_decision(self):
        adapter = FixtureAdapter({("root", "fire"): ("end", 1.)}, {"root": 0, "end": 0})
        search = PuctSearch(adapter, lambda _: {"fire": 1.}, lambda _: {}, lambda _: 0.)
        root = search.run(1)
        with self.assertRaisesRegex(ValueError, "opponent decision"):
            search.evaluate_root_replies(root, "fire")
        self.assertEqual(adapter.current, "root")

    def test_opponent_top_four_is_used_for_search_prior(self):
        graph = {("root", "fire"): ("reply", 0)}
        graph.update({("reply", f"a{i}"): ("end", float(i)) for i in range(8)})
        adapter = FixtureAdapter(graph, {"root": 0, "reply": 1, "end": 0})
        search = PuctSearch(
            adapter, lambda _: {"fire": 1}, lambda _: {f"a{i}": 8 - i for i in range(8)},
            lambda _: 0,
        )
        root = search.run(20)
        opponent = next(iter(root.children.values()))
        self.assertEqual(set(opponent.prior), {"a0", "a1", "a2", "a3"})
        self.assertEqual(set(opponent.policy()), {f"a{i}" for i in range(8)})
        self.assertAlmostEqual(sum(opponent.policy().values()), 1)


class FakeNative:
    def __init__(self):
        self.state = 0
        self.handles = {}
        self.next_handle = 1
        self.max_handles_seen = 0
        self.queue = []

    def _request(self, command):
        if command != "state-digest":
            raise ValueError(command)
        return {"sha256": hashlib.sha256(str(self.state).encode()).hexdigest()}

    def observe(self):
        return {"tick": self.state, "generation": 1, "stateEpoch": 1}

    def create_snapshot(self):
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = self.state
        self.max_handles_seen = max(self.max_handles_seen, len(self.handles))
        return {"handle": handle, "tick": self.state}

    def restore(self, handle):
        self.state = self.handles[handle]
        return self.observe()

    def release_snapshot(self, handle):
        del self.handles[handle]

    def clear_replay_schedule(self):
        self.queue.clear()

    def schedule_replay_card_at_tick(self, **entry):
        self.queue.append({"kind": "card", **entry})

    def schedule_replay_ability_at_tick(self, **entry):
        self.queue.append({"kind": "ability", **entry})


class NativeAdapterTests(unittest.TestCase):
    def make_adapter(self):
        native = FakeNative()
        sidecar = {"reward": 0.0, "cursor": 0, "hidden": 7, "pending": None}

        def capture():
            return SearchState(
                native_snapshot={}, observation={"tick": native.state, "truncated": False},
                actor=0, legal_actions=("left", "right") if native.state == 0 else (),
                external_queue=tuple(copy.deepcopy(native.queue)),
                reward_ledger={"reward": sidecar["reward"]},
                event_cursor={"sequence": sidecar["cursor"]},
                policy_state={"hidden": sidecar["hidden"]}, terminal=native.state != 0,
            )

        def restore(state):
            sidecar.update(
                reward=state.reward_ledger["reward"],
                cursor=state.event_cursor["sequence"],
                hidden=state.policy_state["hidden"], pending=None,
            )

        def apply(action):
            sidecar["pending"] = action

        def advance(max_points, max_seconds):
            native.state = 1 if sidecar["pending"] == "left" else 2
            sidecar.update(reward=float(native.state), cursor=1, hidden=8, pending=None)
            return float(native.state), 1, .25

        return NativeSnapshotSearchAdapter(
            native, capture=capture, restore_sidecar=restore, apply=apply,
            advance=advance,
        ), native, sidecar

    def test_aba_nested_and_handle_lifecycle(self):
        adapter, native, sidecar = self.make_adapter()
        root = adapter.save_search_state()
        adapter.apply_action("left")
        left = adapter.advance_until_decision(6, 10).state
        adapter.restore_search_state(root)
        adapter.apply_action("right")
        right = adapter.advance_until_decision(6, 10).state
        self.assertNotEqual(state_digest(left), state_digest(right))
        adapter.restore_search_state(root)
        adapter.apply_action("left")
        again = adapter.advance_until_decision(6, 10).state
        self.assertEqual(state_digest(left), state_digest(again))
        adapter.restore_search_state(left)
        self.assertEqual((native.state, sidecar["reward"], sidecar["cursor"]), (1, 1, 1))
        adapter.restore_search_state(root)
        self.assertEqual((native.state, sidecar["reward"], sidecar["cursor"]), (0, 0, 0))
        for state in (left, right, again, root):
            adapter.release_search_state(state)
        self.assertEqual(adapter.owned_snapshot_count, 0)

    def test_queue_and_ability_rebuild_and_search_release(self):
        adapter, native, _ = self.make_adapter()
        card = {"kind": "card", "owner": 0, "card_id": 26000021,
                "x": 3500, "y": 14000, "execute_tick": 20}
        ability = {"kind": "ability", "owner": 0,
                   "ability_name_hints": ["hero"], "execute_tick": 21}
        native.queue.extend([card, ability])
        root = adapter.save_search_state()
        native.queue.append({**card, "execute_tick": 30})
        adapter.restore_search_state(root)
        self.assertEqual(native.queue, [card, ability])
        adapter.release_search_state(root)
        native.queue.clear()
        search = PuctSearch(
            adapter, lambda _: {"left": .6, "right": .4}, lambda _: {}, lambda _: 0,
        )
        root_node = search.run(128)
        self.assertEqual(adapter.owned_snapshot_count, 0)
        self.assertLessEqual(native.max_handles_seen, 64)
        search.release(root_node)
        self.assertEqual(adapter.owned_snapshot_count, 0)

    def test_optimized_search_compacts_transient_child_views(self):
        adapter, native, _ = self.make_adapter()

        def capture_view():
            return SearchView.from_state(adapter.capture(), execution_digest=adapter._native_digest())

        adapter.capture_current_view = capture_view
        checkpoint = adapter.checkpoint_restorable()
        root_view = adapter.capture_current_view()
        search = PuctSearch(
            adapter, lambda _: {"left": .6, "right": .4}, lambda _: {}, lambda _: 0,
            compact_children=True,
        )
        root = search.run(8, root_state=root_view, root_checkpoint=checkpoint)
        self.assertTrue(root.children)
        for child in root.children.values():
            self.assertEqual(child.state.observation, {"tick": child.state.observation["tick"]})
            self.assertEqual(child.state.policy_state, {})
            self.assertEqual(child.state.reward_ledger, {})
        adapter.release(checkpoint)
        self.assertEqual(adapter.owned_snapshot_count, 0)

    def test_transient_view_callback_skips_full_capture_and_resets_root_profile(self):
        native = FakeNative()
        view = SearchView(
            observation={"tick": 0, "truncated": False}, actor=0,
            legal_actions=("wait",), external_queue=(), reward_ledger={},
            event_cursor={}, policy_state={},
        )

        def unused_capture():
            raise AssertionError("transient view must not capture restorable state")

        adapter = NativeSnapshotSearchAdapter(
            native, capture=unused_capture, restore_sidecar=lambda _: None,
            apply=lambda _: None, advance=lambda *_: (0., 1, 0.),
            capture_view=lambda: view, profile_enabled=True,
        )
        adapter.begin_profile_root("root-0")
        captured = adapter.capture_current_view()
        self.assertEqual(captured.execution_digest, native._request("state-digest")["sha256"])
        self.assertEqual(adapter.profile_events[0]["root_id"], "root-0")
        adapter.begin_profile_root("root-1")
        self.assertEqual(adapter.profile_events, [])

    def test_invalid_root_prior_releases_native_snapshot(self):
        adapter, native, _ = self.make_adapter()
        search = PuctSearch(adapter, lambda _: {"not-legal": 1}, lambda _: {}, lambda _: 0)
        with self.assertRaisesRegex(ValueError, "illegal action"):
            search.run(4)
        self.assertEqual(adapter.owned_snapshot_count, 0)
        self.assertEqual(native.handles, {})

    def test_sidecar_captures_new_mutable_fields(self):
        class Env:
            def __init__(self):
                self.native = object()
                self._raw = {"tick": 0}
                self._events = []
                self._pending = []
                self._last_rewards = {0: 0}
                self._last_potential = {0: 0}
                self.new_cursor = {"sequence": 5}

        env = Env()
        sidecar = BattleEnvSidecar(env)
        saved = sidecar.capture()
        env.new_cursor["sequence"] = 20
        sidecar.restore(saved)
        self.assertEqual(env.new_cursor, {"sequence": 5})


class DistillationTests(unittest.TestCase):
    @staticmethod
    def record(episode, decision):
        return TeacherRecord(
            episode_id=episode, decision_index=decision,
            observation={"tick": decision}, legal_actions=("wait", "hog"),
            visit_counts={"wait": 16, "hog": 16}, teacher_policy={"wait": .5, "hog": .5},
            q_expected=.1, q_robust=-.1, q_target=.04, terminal_result=1.,
            opponent_sha256="a" * 64, search_simulations=32, teacher_temperature=1.,
            opponent_temperature=.5, state_mode="deterministic_full_state", seed=3,
            native_runtime_sha256="b" * 64, content_pack_sha256="c" * 64,
        )

    def test_episode_assignment_is_stable_across_batches(self):
        batch = [self.record(f"game-{n}", d) for n in range(100) for d in (0, 1)]
        combined = split_episodes(batch)
        first = split_episodes(batch[:100])
        second = split_episodes(batch[100:])
        for name in combined:
            self.assertEqual({r.episode_id for r in combined[name]},
                             {r.episode_id for r in first[name] + second[name]})
        with self.assertRaisesRegex(ValueError, "duplicate teacher decision"):
            split_episodes([batch[0], batch[0]])

    def test_illegal_teacher_probability_is_rejected(self):
        log_probs = torch.log_softmax(torch.tensor([[0., 1.]]), -1)
        with self.assertRaisesRegex(ValueError, "teacher distribution"):
            distillation_loss(
                log_probs, torch.tensor([[.9, .1]]), torch.tensor([0.]),
                torch.tensor([0.]), policy_weight=.5, value_weight=.1,
                legal_mask=torch.tensor([[True, False]]),
            )

    def test_captured_model_inputs_survive_teacher_json_round_trip(self):
        frame = _batch(batch_size=1)
        masks = {
            "gate_mask": torch.tensor([[True, True]]),
            "card_mask": torch.ones(1, 8, dtype=torch.bool),
            "cell_masks": torch.ones(1, 8, 576, dtype=torch.bool),
            "delay_mask": torch.ones(1, 8, 31, dtype=torch.bool),
        }
        encoded_frame, encoded_masks = encode_model_inputs(frame, masks)
        original = replace(self.record("game", 0), semantic_frame=encoded_frame,
                           action_masks=encoded_masks)
        restored = TeacherRecord.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(original.to_dict()["schema"], "hog-tree-teacher-record.v2")
        model_frame, model_masks = restored.model_inputs("cpu")
        self.assertEqual(tensor_record_sha256(model_frame), tensor_record_sha256(frame))
        for name in masks:
            self.assertTrue(torch.equal(model_masks[name], masks[name]))

    def test_legacy_teacher_record_loads_but_has_no_training_inputs(self):
        legacy = self.record("game", 0).to_dict()
        self.assertEqual(legacy["schema"], "hog-tree-teacher-record.v1")
        self.assertNotIn("semantic_frame", legacy)
        restored = TeacherRecord.from_dict(json.loads(json.dumps(legacy)))
        with self.assertRaisesRegex(ValueError, "no captured semantic frame"):
            restored.model_inputs("cpu")
        with self.assertRaisesRegex(ValueError, "requires model inputs"):
            TeacherRecord.from_dict({**legacy, "schema": "hog-tree-teacher-record.v2",
                                     "semantic_frame": None, "action_masks": None})

    def test_training_inputs_reject_missing_or_mismatched_masks(self):
        with self.assertRaisesRegex(ValueError, "no captured semantic frame"):
            self.record("game", 0).model_inputs("cpu")
        frame = _batch(batch_size=1)
        encoded_frame, encoded_masks = encode_model_inputs(frame, {
            "gate_mask": torch.tensor([[True, True]]),
            "card_mask": torch.ones(1, 8, dtype=torch.bool),
            "cell_masks": torch.ones(1, 8, 576, dtype=torch.bool),
            "delay_mask": torch.ones(1, 8, 31, dtype=torch.bool),
        })
        encoded_masks["items"][0][1]["shape"] = [2, 8]
        record = replace(self.record("game", 0), semantic_frame=encoded_frame,
                         action_masks=encoded_masks)
        with self.assertRaisesRegex(ValueError, "shape mismatch"):
            record.validate()
        with self.assertRaisesRegex(ValueError, "do not match the semantic frame"):
            encode_model_inputs(frame, {
                "gate_mask": torch.tensor([[True, True]]),
                "card_mask": torch.ones(1, 8, dtype=torch.bool),
                "cell_masks": torch.ones(1, 8, dtype=torch.bool),
                "delay_mask": torch.ones(1, 8, 31, dtype=torch.bool),
            })


class PolicySignatureTests(unittest.TestCase):
    @dataclass
    class _Record:
        features: torch.Tensor
        tag: str

    def test_tensor_record_signature_covers_dtype_shape_and_value(self):
        baseline = self._Record(torch.tensor([[1, 2]], dtype=torch.int64), "v4")
        self.assertEqual(tensor_record_sha256(baseline), tensor_record_sha256(
            self._Record(torch.tensor([[1, 2]], dtype=torch.int64), "v4")))
        self.assertNotEqual(tensor_record_sha256(baseline), tensor_record_sha256(
            self._Record(torch.tensor([[1, 2]], dtype=torch.int32), "v4")))
        self.assertNotEqual(tensor_record_sha256(baseline), tensor_record_sha256(
            self._Record(torch.tensor([[2, 1]], dtype=torch.int64), "v4")))


if __name__ == "__main__":
    unittest.main()
