from copy import deepcopy
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from types import SimpleNamespace
import time

from ..fair_search.belief import initial_draw
from ..fair_search.contracts import semantic_digest
from ..fair_search.native import NativeBeliefWorld
from .deck_belief import DrawCompletion
from .history_belief import HistoryReplaySession
from .history_provider import HistorySearchProvider, RecoveringHistoryCursor
from .prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from .rollout import RolloutSnapshot
from .search_runtime import RolloutSearchWorld


class UnsupportedLocalBoundary(ValueError):
    pass


def local_state_diagnostics(before_information, after_information, before_view, after_view,
        before_clock, after_clock, before_observation, after_observation):
    differences = []
    def compare(before, after, path):
        if semantic_digest(before) == semantic_digest(after) or len(differences) >= 48:
            return
        if is_dataclass(before) and type(before) is type(after):
            for field in fields(before):
                compare(getattr(before, field.name), getattr(after, field.name), f"{path}.{field.name}")
        elif isinstance(before, (tuple, list)) and isinstance(after, (tuple, list)) and len(before) == len(after):
            for index, (old, new) in enumerate(zip(before, after)):
                compare(old, new, f"{path}[{index}]")
        elif isinstance(before, Mapping) and isinstance(after, Mapping) and before.keys() == after.keys():
            for key in before:
                compare(before[key], after[key], f"{path}.{key}")
        else:
            differences.append(dict(path=path, before=repr(before)[:512], after=repr(after)[:512]))
    compare(before_observation, after_observation, "observation")
    def info_parts(info):
        return dict(sha=info.sha256, frame=semantic_digest(info.frame),
            legality=semantic_digest(dict(card_ids=info.legality.card_ids,
                plays=info.legality.plays, modes=info.legality.modes)))
    before_fields, after_fields = {}, {}
    if is_dataclass(before_observation) and type(before_observation) is type(after_observation):
        for field in fields(before_observation):
            name = field.name
            before_fields[name] = semantic_digest(getattr(before_observation, name))
            after_fields[name] = semantic_digest(getattr(after_observation, name))
    return dict(
        information_before=info_parts(before_information),
        information_after=info_parts(after_information),
        view_before=before_view,
        view_after=after_view,
        observation_before=semantic_digest(before_observation),
        observation_after=semantic_digest(after_observation),
        changed_observation_fields={name: dict(before=before_fields[name], after=after_fields[name])
            for name in before_fields if before_fields[name] != after_fields[name]},
        observation_differences=differences,
        clock_before=semantic_digest(before_clock),
        clock_after=semantic_digest(after_clock),
    )


def native_draw_fields(observation, completion, *, owner):
    if type(owner) is not int or owner not in (0, 1) or not isinstance(completion, DrawCompletion):
        raise ValueError("local conditioning requires a typed opponent completion")
    expected, replacement = [], []
    installed = None
    for current_owner in (0, 1):
        players = [player for player in observation["players"] if player["owner"] == current_owner]
        if len(players) != 1:
            raise ValueError("native conditioning player identity differs")
        player = players[0]
        if len(player["hand"]) != 4 or len(player["cycle"]) != 4:
            raise UnsupportedLocalBoundary("local draw replacement requires completed native refills")
        draw, slots = initial_draw(player)
        mapping = {card["cardId"]: card["deckSlot"] for card in player["deck"]}
        if (len(mapping) != 8 or set(mapping.values()) != set(range(8))
                or set(draw) != set(completion.deck) or set(mapping) != set(completion.deck)):
            raise ValueError("native local conditioning requires the same eight base cards")
        expected.extend((*slots, *([-2] * 4)))
        if current_owner == 1 - owner:
            installed = tuple(sorted(completion.current_draw[:4])) + completion.current_draw[4:]
            replacement.extend((*(mapping[card] for card in installed), *([-2] * 4)))
        else:
            replacement.extend((*slots, *([-2] * 4)))
    return [observation["generation"], observation["stateEpoch"], observation["tick"],
        *expected, *replacement], installed


def refresh_local_information(rollout):
    environment = rollout.runtime.environment
    observations = environment.observe_dual(policy_features_only=True)
    rollout.runtime.frozen = {owner: observations[owner] for owner in rollout.runtime.clock.ready}
    rollout.information = {}
    for tensorizer in rollout.tensorizers.values():
        for name in RolloutSnapshot.tensorizer_caches:
            getattr(tensorizer, name).clear()
    return rollout.freeze_due_information()


class SettledDrawReconditioner:
    schema = "fair-final-settled-local-draw.v1"

    def __init__(self, factory):
        if not isinstance(factory, PriorHistoryBeliefFactory) or factory.closed:
            raise ValueError("local conditioning requires an open verified independent prior factory")
        self.factory = factory
        self.cursors = {}
        self.sampler_sha = semantic_digest(dict(schema=self.schema, prior_sha=factory.prior.sha256,
            factory_sha=factory.sampler_sha, boundary="both-ready-no-pending-complete-refills",
            hand_slots="canonical-card-order-native-legality-authoritative",
            conditioning="card-cycle-only-retain-independent-public-anchor",
            training_admissible=False))

    def apply(self, session, history, information, *, seed, check_deadline=lambda: None):
        if (self.factory.closed or not isinstance(session, HistoryReplaySession) or session.closed
                or session.history != history or session.owner != information.owner
                or information.tick != history.root_tick or information.sha256 != history.root_fair_sha):
            raise ValueError("local conditioning root differs from its verified replay session")
        manifest = getattr(session, "conditioning_manifest", {})
        if (manifest.get("source") != "independent-belief" or manifest.get("actual_state_copied") is not False
                or manifest.get("sampler_sha") != self.factory.sampler_sha
                or manifest.get("prior_sha") != self.factory.prior.sha256
                or manifest.get("native_draw_verified") is not True
                or session.opponent is not self.factory.opponent):
            raise ValueError("local conditioning requires independent native construction evidence")
        rollout = session.rollout
        runtime = rollout.runtime
        environment, owner = runtime.environment, session.owner
        if (runtime.failed or runtime.ended or runtime.truncated or runtime.clock.ready != {0, 1}
                or runtime.clock.pending or runtime.clock.tick != information.tick):
            raise UnsupportedLocalBoundary("local conditioning requires both settled decision boundaries")
        if environment._pending_abilities or any(item.status == "queued" for item in environment._pending):
            raise UnsupportedLocalBoundary("local conditioning rejects queued native commands")
        check_deadline()
        started = time.monotonic()
        cursor = self.cursors.get(owner)
        if cursor is None:
            cursor = HistoryCycleCursor(self.factory.prior, opening=history.opening)
        completion = cursor.sample(history, seed=seed)
        self.cursors[owner] = cursor
        native = environment.native
        original = native.observe()
        original_rich = environment._rich_snapshot_for(original)
        fields, installed = native_draw_fields(original, completion, owner=owner)
        before_information = rollout.freeze_due_information()
        if before_information[owner].sha256 != information.sha256:
            raise ValueError("independent anchor changed before local conditioning")
        before_view = semantic_digest(runtime.frozen[owner])
        before_observation = runtime.frozen[owner]
        before_clock = runtime.clock.capture()
        own_draw = initial_draw(next(player for player in original["players"] if player["owner"] == owner))
        with rollout.snapshot(self.factory.sidecar_factory) as rollback:
            try:
                check_deadline()
                receipt = native._request("belief-draw-probe " + " ".join(map(str, fields)))
                if (receipt.get("ok") is not True or receipt.get("experimental") is not True
                        or receipt.get("generation") != original["generation"]
                        or receipt.get("tick") != original["tick"]
                        or type(receipt.get("stateEpoch")) is not int
                        or receipt["stateEpoch"] <= original["stateEpoch"]):
                    raise ValueError("local native draw receipt differs")
                environment._raw = dict(native.observe())
                environment._invalidate_rich_cache()
                restore_receipt = SimpleNamespace(generation=receipt["generation"],
                    state_epoch=receipt["stateEpoch"], tick=receipt["tick"])
                NativeBeliefWorld._rebase_native_cursors(SimpleNamespace(runtime=runtime),
                    environment._rich_snapshot_for(environment.raw_observation), restore_receipt)
                actual = environment.raw_observation
                current = initial_draw(next(player for player in actual["players"] if player["owner"] == 1-owner))[0]
                if current != installed or initial_draw(next(player for player in actual["players"]
                        if player["owner"] == owner)) != own_draw:
                    raise ValueError("local native replacement changed its exact draw contract")
                updated = refresh_local_information(rollout)
                fair_unchanged = updated[owner].sha256 == information.sha256
                view_unchanged = semantic_digest(runtime.frozen[owner]) == before_view
                clock_unchanged = runtime.clock.capture() == before_clock
                if not (fair_unchanged and view_unchanged and clock_unchanged):
                    diagnostics = local_state_diagnostics(information, updated[owner], before_view,
                        semantic_digest(runtime.frozen[owner]), before_clock, runtime.clock.capture(),
                        before_observation, runtime.frozen[owner])
                    current_rich = environment._rich_snapshot_for(actual)
                    diagnostics["native_phase_before"] = [repr(item.phase_runtime) for item in
                        original_rich.objects if item is not None and item.phase_runtime is not None]
                    diagnostics["native_phase_after"] = [repr(item.phase_runtime) for item in
                        current_rich.objects if item is not None and item.phase_runtime is not None]
                    raise ValueError("local conditioning changed root state: "
                        f"fair={fair_unchanged} view={view_unchanged} clock={clock_unchanged} "
                        f"diagnostics={diagnostics}")
                check_deadline()
                evidence = dict(schema=self.schema, source="independent-belief", actual_state_copied=False,
                    root_fair_sha=information.sha256, history_sha=history.sha256, owner=owner, root_tick=information.tick,
                    sampler_sha=self.sampler_sha, prior_sha=self.factory.prior.sha256,
                    seed_commitment=semantic_digest([seed]), completion_manifest=deepcopy(completion.manifest),
                    native_current_draw=list(current), native_draw_verified=True, public_and_own_fair_unchanged=True,
                    controller_unchanged=True, history_ticks_replayed=0,
                    opponent_fair_sha=updated[1-owner].sha256,
                    opponent_controller_sha=self.factory.opponent.controller_sha,
                    opponent_checkpoint_sha=self.factory.opponent.checkpoint_sha,
                    scope="settled-root-local-draw-engineering", training_admissible=False)
                return dict(manifest=evidence, completion=completion, seconds=time.monotonic()-started,
                    cycle_delta=deepcopy(cursor.last_delta))
            except BaseException:
                rollback.restore()
                raise


class SettledLocalSearchProvider(HistorySearchProvider):
    def __init__(self, factory, *, history, anchor_seed, attempts=4, check_deadline=lambda: None):
        self.conditioner = SettledDrawReconditioner(factory)
        super().__init__(factory, history=history, attempts=attempts, check_deadline=check_deadline)
        self.cursor = RecoveringHistoryCursor(factory, seed=anchor_seed, attempts=attempts,
            check_deadline=check_deadline)
        self.sampler_sha = semantic_digest(dict(schema="fair-final-settled-local-search.v1",
            conditioner_sha=self.conditioner.sampler_sha, anchor_seed_commitment=semantic_digest([anchor_seed]),
            attempts=attempts, anchor="independent-incremental-history-with-bounded-rebuild",
            root_draw="fresh-public-cycle-completion", training_admissible=False))

    def build(self, information, *, seed):
        if self.closed or self.active is not None:
            raise ValueError("local provider is closed or already leased")
        world = None
        try:
            session = self.cursor.sync(self.history, information)
            result = self.conditioner.apply(session, self.history, information, seed=seed,
                check_deadline=self.check_deadline)
            manifest = dict(result["manifest"], sampler_sha=self.sampler_sha,
                conditioning=result["manifest"], anchor=deepcopy(session.conditioning_manifest),
                anchor_history_sha=session.history.sha256,
                root_sampling="fresh-draw-on-independent-incremental-anchor")
            world = RolloutSearchWorld(session.rollout, owner=information.owner,
                sidecar_factory=self.factory.sidecar_factory, manifest=manifest)
            world.local_profile = dict(seconds=result["seconds"], cycle_delta=result["cycle_delta"],
                anchor_event=deepcopy(self.cursor.events[-1]))
            original_close = world.close
            released = False

            def close():
                nonlocal released
                if released:
                    return
                released = True
                try:
                    try:
                        original_close()
                    finally:
                        session.root.restore()
                except BaseException:
                    self.closed = True
                    self.cursor.close()
                    raise
                finally:
                    if self.active is world:
                        self.active = None

            world.close = close
            self.active = world
            return world
        except BaseException:
            try:
                if world is not None:
                    world.close()
            finally:
                self.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self.cursor.close()
