import hashlib
import random

import torch
import torch.nn.functional as functional

from ..event_ppo import _stack_frames
from .contracts import SearchEpisode
from .learner import decode_action
from ..v4.tensors import concatenate_tensor_records


def split_episodes(identities, *, validation_count, seed):
    identities = list(identities)
    if len(identities) != len(set(identities)) or not 0 < validation_count < len(identities):
        raise ValueError("split requires distinct episodes and nonempty partitions")
    ranked = sorted(identities, key=lambda identity: hashlib.sha256(f"{seed}:{identity}".encode()).digest())
    validation = set(ranked[:validation_count])
    return {identity: "validation" if identity in validation else "train" for identity in sorted(identities)}


class EpisodeSampler:
    def __init__(self, *, balanced=True, seed=20261013):
        self.balanced = balanced
        self.rng = random.Random(seed)
        self.episodes = {}
        self.counts = {}
        self.order = {}
        self.cursor = {}

    def add(self, episode):
        if not isinstance(episode, SearchEpisode):
            raise TypeError("consumer requires completed SearchEpisode")
        identity = episode.episode_id
        if identity in self.episodes:
            raise ValueError("duplicate consumer episode")
        self.episodes[identity] = episode
        self.counts[identity] = [0] * len(episode.decisions)
        self.order[identity] = list(range(len(episode.decisions)))
        self.cursor[identity] = 0
        if self.balanced:
            self.rng.shuffle(self.order[identity])

    def sample(self, count):
        if not self.episodes or type(count) is not int or count < 1:
            raise ValueError("nonempty replay and positive sample count required")
        identities = tuple(self.episodes)
        totals = {identity: sum(self.counts[identity]) for identity in identities}
        rows = []
        for ordinal in range(count):
            minimum = min(totals.values()) if self.balanced else None
            candidates = [identity for identity in identities if totals[identity] == minimum] if self.balanced else identities
            identity = self.rng.choice(candidates)
            if self.balanced:
                if self.cursor[identity] == len(self.order[identity]):
                    self.rng.shuffle(self.order[identity])
                    self.cursor[identity] = 0
                index = self.order[identity][self.cursor[identity]]
                self.cursor[identity] += 1
            else:
                index = self.rng.randrange(len(self.counts[identity]))
            self.counts[identity][index] += 1
            totals[identity] += 1
            rows.append(self.episodes[identity].decisions[index])
        return rows

    def state_dict(self):
        return dict(schema="fair-consumer-sampler.v1", balanced=self.balanced, rng=self.rng.getstate(),
                    identities=list(self.episodes), counts={key: list(value) for key, value in self.counts.items()},
                    order={key: list(value) for key, value in self.order.items()}, cursor=dict(self.cursor))

    def load_state_dict(self, state):
        if (set(state) != {"schema", "balanced", "rng", "identities", "counts", "order", "cursor"}
                or state["schema"] != "fair-consumer-sampler.v1" or state["balanced"] != self.balanced
                or state["identities"] != list(self.episodes)):
            raise ValueError("consumer sampler identity differs")
        for field in ("counts", "order", "cursor"):
            if set(state[field]) != set(self.episodes):
                raise ValueError("consumer sampler episode fields differ")
        for identity, episode in self.episodes.items():
            size = len(episode.decisions)
            counts, order, cursor = state["counts"][identity], state["order"][identity], state["cursor"][identity]
            if (len(counts) != size or any(type(value) is not int or value < 0 for value in counts)
                    or any(type(value) is not int for value in order)
                    or sorted(order) != list(range(size)) or type(cursor) is not int or not 0 <= cursor <= size):
                raise ValueError("invalid consumer sampler cursor")
            if self.balanced:
                cycles, remainder = divmod(sum(counts), size)
                expected = [cycles] * size
                for index in order[:remainder]:
                    expected[index] += 1
                if (cursor % size != remainder or counts != expected
                        or (cursor == 0 and sum(counts) != 0)):
                    raise ValueError("consumer row coverage differs from cursor")
        probe = random.Random()
        probe.setstate(state["rng"])
        self.rng.setstate(state["rng"])
        self.counts = {key: list(value) for key, value in state["counts"].items()}
        self.order = {key: list(value) for key, value in state["order"].items()}
        self.cursor = dict(state["cursor"])

    def coverage(self):
        episodes = {identity: dict(rows=len(counts), draws=sum(counts), unseen=sum(value == 0 for value in counts))
                    for identity, counts in self.counts.items()}
        return dict(episodes=episodes, rows=sum(row["rows"] for row in episodes.values()),
                    draws=sum(row["draws"] for row in episodes.values()),
                    unseen=sum(row["unseen"] for row in episodes.values()))


@torch.no_grad()
def evaluate_decisions(model, decisions, *, batch_size=32):
    model.eval()
    device = next(model.parameters()).device
    output = []
    for start in range(0, len(decisions), batch_size):
        rows = decisions[start:start + batch_size]
        context, logits = model.encode(_stack_frames(tuple(row.information.frame for row in rows)))
        masks = {name: torch.cat([row.information.masks[name] for row in rows]).to(device)
                 for name in rows[0].information.masks}
        indices, actions, weights = [], [], []
        for index, row in enumerate(rows):
            for action, count in row.visits.items():
                if count:
                    indices.append(index)
                    actions.append(decode_action(action, device))
                    weights.append(count / 32.)
        indices = torch.tensor(indices, device=device)
        weights = torch.tensor(weights, device=device)
        step = model.policy.evaluate_actions(context.index_select(indices), concatenate_tensor_records(actions),
                    **{name: value.index_select(0, indices) for name, value in masks.items()}, validate=True)
        ce = torch.zeros(len(rows), device=device).index_add_(0, indices, -weights * step.log_prob)
        entropy = torch.zeros(len(rows), device=device).index_add_(0, indices, -weights * weights.log())
        top_indices = []
        offset = 0
        for row in rows:
            positive = [action for action, count in row.visits.items() if count]
            top = min(row.visits, key=lambda action: (-row.visits[action], action))
            top_indices.append(offset + positive.index(top))
            offset += len(positive)
        top_ce = -step.log_prob[torch.tensor(top_indices, device=device)]
        value_ce = functional.cross_entropy(logits, torch.tensor([{1: 0, 0: 1, -1: 2}[row.result]
                    for row in rows], device=device), reduction="none")
        greedy = model.policy.select_action(context, **masks, sample=False, validate=True).action
        for index, row in enumerate(rows):
            key = "wait" if not bool(greedy.act[index]) else "play:" + ":".join(
                str(int(component[index])) for component in (greedy.card_row, greedy.cell, greedy.delay_bin))
            top = min(row.visits, key=lambda action: (-row.visits[action], action))
            played = top != "wait"
            components = key.split(":"), top.split(":")
            output.append(dict(policy_ce=float(ce[index]), policy_kl=float(ce[index] - entropy[index]),
                    top_action_ce=float(top_ce[index]),
                    value_ce=float(value_ce[index]), greedy_top=float(key == top),
                    play_top=float(key == top) if played else None,
                    wait_top=float(key == top) if not played else None,
                    card_match=float(key != "wait" and components[0][1] == components[1][1]) if played else None,
                    cell_match=float(key != "wait" and components[0][2] == components[1][2]) if played else None,
                    delay_match=float(key != "wait" and components[0][3] == components[1][3]) if played else None,
                    owner=row.information.owner, tick=row.information.tick))
    return output


def summarize_decisions(records):
    keys = ("policy_ce", "policy_kl", "top_action_ce", "value_ce", "greedy_top", "play_top", "wait_top",
            "card_match", "cell_match", "delay_match")
    return {key: sum(values) / len(values) if values else None
            for key in keys for values in [[row[key] for row in records if row[key] is not None]]}


def evaluate_episodes(model, episodes):
    records = {episode.episode_id: evaluate_decisions(model, episode.decisions) for episode in episodes}
    summaries = {identity: summarize_decisions(rows) for identity, rows in records.items()}
    groups = {}
    selectors = {"owner0": lambda row: row["owner"] == 0, "owner1": lambda row: row["owner"] == 1,
                 "early": lambda row: row["tick"] < 1200,
                 "middle": lambda row: 1200 <= row["tick"] < 3600,
                 "late": lambda row: row["tick"] >= 3600}
    for name, select in selectors.items():
        groups[name] = summarize_decisions([summarize_decisions([row for row in rows if select(row)])
                                           for rows in records.values()])
    return dict(equal_episode=summarize_decisions(list(summaries.values())), per_episode=summaries,
                groups=groups, rows=sum(len(rows) for rows in records.values()))
