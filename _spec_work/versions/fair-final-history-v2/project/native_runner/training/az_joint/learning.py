from collections import OrderedDict
import math
from pathlib import Path
import random

import torch
import torch.nn.functional as functional

from ..event_ppo import _stack_frames
from ..fair_search.consumer import EpisodeSampler
from ..fair_search.contracts import semantic_digest
from .codec import JointActionCodec
from .data import AZJointEpisode, JointMigrationEpisode, load_episode


class EpisodeCache:
    def __init__(self, *, migration, capacity=4):
        self.migration, self.capacity = migration, capacity
        self.cache = OrderedDict()

    def load(self, marker):
        marker = str(Path(marker).resolve())
        if marker not in self.cache:
            self.cache[marker] = load_episode(marker, migration=self.migration)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
        self.cache.move_to_end(marker)
        return self.cache[marker]


class EpisodeReference:
    def __init__(self, marker, cache):
        episode = cache.load(marker)
        self.marker, self.cache = str(Path(marker).resolve()), cache
        self.episode_id, self.split = episode.episode_id, episode.split
        self.generation = episode.generation
        self.rows = len(episode.decisions)
        self.identity = semantic_digest(episode.to_payload())
        self.decisions = self

    def __len__(self):
        return self.rows

    def __getitem__(self, index):
        return self.cache.load(self.marker).decisions[index]


class JointSampler(EpisodeSampler):
    def add(self, episode):
        if not isinstance(episode, (JointMigrationEpisode, AZJointEpisode, EpisodeReference)):
            raise TypeError("joint sampler rejects legacy data")
        if episode.split != "train" or episode.episode_id in self.episodes:
            raise ValueError("only distinct training episodes enter optimization")
        identity = episode.episode_id
        self.episodes[identity] = episode
        self.counts[identity] = [0] * len(episode.decisions)
        self.order[identity] = list(range(len(episode.decisions)))
        self.cursor[identity] = 0
        self.rng.shuffle(self.order[identity])

    def sample_indices(self, count):
        if not self.episodes or type(count) is not int or count <= 0:
            raise ValueError("invalid joint sample request")
        totals = {identity: sum(counts) for identity, counts in self.counts.items()}
        selected = []
        for ordinal in range(count):
            minimum = min(totals.values())
            identity = self.rng.choice([key for key in totals if totals[key] == minimum])
            order = self.order[identity]
            if self.cursor[identity] == len(order):
                self.rng.shuffle(order)
                self.cursor[identity] = 0
            index = order[self.cursor[identity]]
            self.cursor[identity] += 1
            self.counts[identity][index] += 1
            totals[identity] += 1
            selected.append((identity, index))
        return selected


def batch_inputs(rows, device):
    frame = _stack_frames(tuple(row.frame for row in rows))
    masks = {name: torch.cat([row.masks[name] for row in rows]).to(device) for name in rows[0].masks}
    return frame, masks


def decision_losses(policy_logits, wdl_logits, rows, *, migration):
    if policy_logits.shape != (len(rows), JointActionCodec.size) or wdl_logits.shape != (len(rows), 3):
        raise ValueError("joint loss shape differs")
    normalizer = policy_logits.logsumexp(-1)
    log_wdl = wdl_logits.log_softmax(-1)
    if migration:
        if any(row.teacher_action is None or row.visits is not None for row in rows):
            raise ValueError("migration loss rejects search targets")
        actions = torch.tensor([row.teacher_action for row in rows], device=policy_logits.device)
        policy = normalizer - policy_logits.gather(1, actions[:, None]).squeeze(1)
        target = torch.tensor([row.teacher_wdl for row in rows], device=wdl_logits.device)
        value = (torch.xlogy(target, target) - target * log_wdl).sum(-1)
        entropy = torch.zeros_like(policy)
    else:
        if any(row.visits is None or row.teacher_action is not None for row in rows):
            raise ValueError("formal loss rejects migration targets")
        policy, entropy = [], []
        for index, row in enumerate(rows):
            actions = torch.tensor([action for action, count in row.visits], device=policy_logits.device)
            weights = torch.tensor([count / 128 for action, count in row.visits], device=policy_logits.device)
            policy.append(normalizer[index] - (weights * policy_logits[index, actions]).sum())
            entropy.append(-(weights * weights.log()).sum())
        policy, entropy = torch.stack(policy), torch.stack(entropy)
        value = functional.nll_loss(log_wdl, torch.tensor([row.label for row in rows],
                                                         device=wdl_logits.device), reduction="none")
    if not bool(torch.isfinite(policy + value).all()):
        raise ValueError("nonfinite joint training loss or illegal target")
    return policy, value, policy - entropy


def update_batch(model, optimizer, sampler, config, *, migration):
    indices = sampler.sample_indices(config.batch_size)
    indices.sort(key=lambda item: item[0])
    optimizer.zero_grad(set_to_none=True)
    total = [0., 0.]
    device = next(model.parameters()).device
    for start in range(0, len(indices), config.micro_batch):
        rows = [sampler.episodes[identity].decisions[index] for identity, index in indices[start:start + config.micro_batch]]
        logits, wdl = model(*batch_inputs(rows, device))
        policy, value, _ = decision_losses(logits, wdl, rows, migration=migration)
        ((policy + value).sum() / config.batch_size).backward()
        total[0] += float(policy.detach().sum()) / config.batch_size
        total[1] += float(value.detach().sum()) / config.batch_size
    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config.gradient_clip, error_if_nonfinite=True)
    optimizer.step()
    return dict(policy_loss=total[0], value_loss=total[1], gradient_norm=float(norm))


def average(records):
    keys = sorted({name for row in records for name, value in row.items() if value is not None})
    return {name: sum(values) / len(values) for name in keys
            for values in [[row[name] for row in records if row.get(name) is not None]] if values}


@torch.inference_mode()
def evaluate(model, episodes, *, migration, batch_size=8, rows_per_episode=None, seed=20261019):
    model.eval()
    device = next(model.parameters()).device
    summaries, grouped = {}, {name: [] for name in ("owner0", "owner1", "early", "middle", "late")}
    for episode in episodes:
        indices = list(range(len(episode.decisions)))
        if rows_per_episode is not None and len(indices) > rows_per_episode:
            random.Random(str(seed) + ":" + episode.episode_id).shuffle(indices)
            indices = sorted(indices[:rows_per_episode])
        records = []
        for start in range(0, len(indices), batch_size):
            rows = [episode.decisions[index] for index in indices[start:start + batch_size]]
            logits, wdl = model(*batch_inputs(rows, device))
            policy, value, kl = decision_losses(logits, wdl, rows, migration=migration)
            predicted = logits.argmax(-1).tolist()
            probabilities = wdl.softmax(-1)
            labels = torch.tensor([row.label for row in rows], device=device)
            value_ce = functional.cross_entropy(wdl, labels, reduction="none")
            brier = (probabilities - functional.one_hot(labels, 3)).square().sum(-1)
            wait = (logits[:, 0] - logits.logsumexp(-1)).exp()
            for index, row in enumerate(rows):
                target = row.teacher_action if migration else min(row.visits, key=lambda item: (-item[1], item[0]))[0]
                truth = JointActionCodec.decode(target)
                action = JointActionCodec.decode(predicted[index])
                record = dict(policy_ce=float(policy[index]), value_loss=float(value[index]),
                    total_loss=float(policy[index] + value[index]), policy_kl=float(kl[index]),
                    full_match=float(predicted[index] == target),
                    act_full_match=float(predicted[index] == target) if truth is not None else None,
                    wait_act_match=float((action is None) == (truth is None)),
                    card_match=float(action is not None and action[0] == truth[0]) if truth else None,
                    cell_match=float(action is not None and action[1] == truth[1]) if truth else None,
                    delay_match=float(action is not None and action[2] == truth[2]) if truth else None,
                    value_ce=float(value_ce[index]), brier=float(brier[index]), wait_probability=float(wait[index]))
                if migration:
                    record["wdl_kl"] = float(value[index])
                    teacher = row.teacher_wdl[0] - row.teacher_wdl[2]
                    record["value_mae"] = abs(float(probabilities[index, 0] - probabilities[index, 2]) - teacher)
                records.append((row.owner, row.tick, record))
        summaries[episode.episode_id] = average([record for owner, tick, record in records])
        for name in grouped:
            selected = [record for owner, tick, record in records if
                (name == "owner0" and owner == 0) or (name == "owner1" and owner == 1)
                or (name == "early" and tick < 1200) or (name == "middle" and 1200 <= tick < 3600)
                or (name == "late" and tick >= 3600)]
            if selected:
                grouped[name].append(average(selected))
    return dict(equal_episode=average(list(summaries.values())), per_episode=summaries,
                groups={name: average(values) for name, values in grouped.items()})


def migration_fit_acceptance(report):
    metrics = report["equal_episode"]
    gates = dict(full_match=metrics.get("full_match", 0) >= .95,
        act_full_match=metrics.get("act_full_match", 0) >= .9,
        wait_act_match=metrics.get("wait_act_match", 0) >= .98,
        wdl_kl=metrics.get("wdl_kl", math.inf) <= .01,
        value_mae=metrics.get("value_mae", math.inf) <= .02)
    return dict(passed=all(gates.values()), gates=gates, metrics=metrics)
