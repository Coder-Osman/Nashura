from dataclasses import replace
from collections.abc import Mapping
import hashlib
import random
import time

from .contracts import InformationState, SearchDecision, SearchEpisode, require_sha
from .search import InformationSetSearch
from .storage import save_episode


def independent_visit_action(visits, episode_id, owner, ordinal):
    if (not visits or any(type(count) is not int or count < 0 for count in visits.values())
            or sum(visits.values()) != 32):
        raise ValueError("independent sampling requires exactly 32 integer visits")
    seed = int.from_bytes(hashlib.sha256(
        f"fair-clean-action.v1:{episode_id}:{owner}:{ordinal}".encode()).digest()[:8], "big")
    actions = sorted(action for action, count in visits.items() if count)
    return random.Random(seed).choices(actions, weights=[visits[action] for action in actions])[0], seed


def collect_episode(runtime, beliefs, inference, *, episode_id, generation, current_sha256,
                    owner_sha256, config, runtime_sha256, destination, attempt=0, max_seconds=43200.,
                    progress=None, independent_visit_sampling=False):
    started = time.monotonic()
    pending = []
    decision_index = 0
    search_seconds = []
    search_particles = []
    owner_ordinals = {0: 0, 1: 0}
    try:
        if independent_visit_sampling and (config.root_noise != 0 or config.late_temperature != 1):
            raise ValueError("clean independent sampling requires zero noise and temperature one")
        require_sha(current_sha256)
        require_sha(runtime_sha256)
        if isinstance(owner_sha256, Mapping):
            if set(owner_sha256) != {0, 1} or any(type(owner) is not int for owner in owner_sha256):
                raise ValueError("episode needs identities for owners zero and one")
            owner_sha256 = tuple(owner_sha256[owner] for owner in (0, 1))
        else:
            owner_sha256 = tuple(owner_sha256)
        if len(owner_sha256) != 2:
            raise ValueError("episode needs two model identities")
        for identity in owner_sha256:
            require_sha(identity)
        while not runtime.ended:
            if time.monotonic() - started >= max_seconds:
                raise TimeoutError("episode deadline is not a terminal draw")
            frozen = runtime.freeze_due_information()
            if not frozen:
                runtime.advance()
                continue
            if any(not isinstance(info, InformationState) or info.owner != owner for owner, info in frozen.items()):
                raise ValueError("runtime returned invalid due information")
            selected = {}
            for owner, information in sorted(frozen.items()):
                seed_payload = f"fair-search-stream.v1:{generation}:{information.sha256}:{decision_index}:{attempt}"
                search_seed = int.from_bytes(hashlib.sha256(seed_payload.encode()).digest()[:8], "big")
                search = InformationSetSearch(beliefs[owner], inference[owner], config=config)
                result = search.run(information, seed=search_seed,
                                    deadline=started + max_seconds, training=not independent_visit_sampling)
                search_seconds.append(result.seconds)
                search_particles.append(result.particles)
                action, action_seed = (independent_visit_action(result.visits, episode_id, owner, owner_ordinals[owner])
                                       if independent_visit_sampling else (result.selected, None))
                if not information.legal(action):
                    raise ValueError("collector selected illegal action")
                selected[owner] = action
                owner_ordinals[owner] += 1
                if owner_sha256[owner] == current_sha256:
                    pending.append(SearchDecision(decision_index, information, result.visits, action,
                                                  current_sha256, config.sha256, runtime_sha256, 0))
                decision_index += 1
                if progress is not None:
                    progress(dict(stage="search_completed", episode_id=episode_id,
                                  decisions=decision_index, owner=owner, tick=information.tick,
                                  visits=sum(result.visits.values()), search_seconds=result.seconds,
                                  particles=result.particles, seconds=time.monotonic() - started,
                                  selected=action, action_seed=action_seed,
                                  owner_ordinal=owner_ordinals[owner] - 1, config_sha256=config.sha256,
                                  visit_counts=result.visits if independent_visit_sampling else None,
                                  timings=result.timings,
                                  inference_profile=dict(getattr(inference[owner], "profile", {})),
                                  belief_profile=dict(getattr(beliefs[owner], "profile", {}))))
            runtime.commit_simultaneous(selected, frozen)
        terminal = runtime.terminal_evidence()
        expected = {"ended", "truncated", "winner", "terminal_tick", "reason", "rejected", "unconfirmed"}
        if set(terminal) != expected or terminal["ended"] is not True or terminal["truncated"] is not False:
            raise ValueError("runtime did not produce exact complete terminal evidence")
        if terminal["rejected"] or terminal["unconfirmed"]:
            raise ValueError("episode execution ledger is not clean")
        winner = terminal["winner"]
        rows = tuple(replace(row, result=0 if winner is None else
                             (1 if row.information.owner == winner else -1)) for row in pending)
        episode = SearchEpisode(episode_id, generation, rows, winner, terminal["terminal_tick"],
                                terminal["reason"], current_sha256, tuple(owner_sha256), attempt)
        manifest = save_episode(destination, episode)
        return dict(manifest=str(manifest), rows=len(rows), decisions=decision_index,
                    seconds=time.monotonic() - started, search_seconds=sum(search_seconds),
                    search_seconds_mean=(sum(search_seconds) / len(search_seconds) if search_seconds else 0.),
                    search_seconds_max=max(search_seconds, default=0.),
                    particles_mean=(sum(search_particles) / len(search_particles) if search_particles else 0.),
                    belief_attempts=sum(getattr(provider, "profile", {}).get("attempts", 0)
                                        for provider in beliefs.values()),
                    belief_accepted=sum(getattr(provider, "profile", {}).get("accepted", 0)
                                        for provider in beliefs.values()),
                    belief_rejected=sum(getattr(provider, "profile", {}).get("rejected", 0)
                                        for provider in beliefs.values()),
                    belief_reserve_reused=sum(getattr(provider, "profile", {}).get("reserve_reused", 0)
                                              for provider in beliefs.values()),
                    belief_reserve_invalidated=sum(getattr(provider, "profile", {}).get("reserve_invalidated", 0)
                                                   for provider in beliefs.values()),
                    belief_rebuild_avoided=sum(getattr(provider, "profile", {}).get("rebuild_avoided", 0)
                                              for provider in beliefs.values()),
                    belief_errors={str(owner): getattr(provider, "profile", {}).get("errors", [])[-8:]
                                   for owner, provider in beliefs.items()},
                    episode_id=episode_id, generation=generation)
    finally:
        failures = []
        try:
            for provider in beliefs.values():
                close = getattr(provider, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as error:
                        failures.append(error)
        finally:
            runtime.close()
        if failures:
            raise RuntimeError("episode belief cleanup failed") from failures[0]
