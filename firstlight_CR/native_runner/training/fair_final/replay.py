from copy import deepcopy
import random

from .contracts import exact_fields, integer


class FrozenReplayCursor:
    schema = "FairFinalReplayCursor/v1"

    def __init__(self, store, match_ids, *, seed, sample_budget):
        integer(seed, 0, "sampler seed")
        integer(sample_budget, 0, "sample budget")
        if not match_ids or len(set(match_ids)) != len(match_ids):
            raise ValueError("distinct completed matches required")
        self.store, self.dataset_sha = store, store.dataset_sha
        self.matches = sorted(match_ids)
        self.rng = random.Random(seed)
        self.entries, self.counts, self.order, self.cursor = {}, {}, {}, {}
        self.budget, self.consumed = sample_budget, 0
        for match_id in self.matches:
            episode, receipt = store.load(match_id)
            if episode.payload["split"] != "train":
                raise ValueError("validation/test/confirmation games never enter optimization")
            size = len(episode.payload["decisions"])
            self.entries[match_id] = dict(sha=episode.sha256, rows=size)
            self.counts[match_id], self.order[match_id], self.cursor[match_id] = [0] * size, list(range(size)), 0
            self.rng.shuffle(self.order[match_id])

    def sample(self, count):
        integer(count, 1, "sample count")
        if self.consumed + count > self.budget:
            raise ValueError("training sample budget exhausted")
        totals = {key: sum(values) for key, values in self.counts.items()}
        selected = []
        for ordinal in range(count):
            minimum = min(totals.values())
            match_id = self.rng.choice([key for key in self.matches if totals[key] == minimum])
            if self.cursor[match_id] == len(self.order[match_id]):
                self.rng.shuffle(self.order[match_id])
                self.cursor[match_id] = 0
            index = self.order[match_id][self.cursor[match_id]]
            self.cursor[match_id] += 1
            self.counts[match_id][index] += 1
            totals[match_id] += 1
            selected.append((match_id, index))
        self.consumed += count
        return selected

    def state_dict(self):
        return deepcopy(dict(schema=self.schema, dataset_sha=self.dataset_sha, matches=self.matches,
            entries=self.entries, counts=self.counts, order=self.order, cursor=self.cursor,
            budget=self.budget, consumed=self.consumed, rng=self.rng.getstate()))

    def load_state_dict(self, state):
        exact_fields(state, self.state_dict().keys(), "replay cursor")
        if (state["schema"] != self.schema or state["dataset_sha"] != self.dataset_sha
                or state["matches"] != self.matches or state["entries"] != self.entries or state["budget"] != self.budget):
            raise ValueError("replay content identity or budget differs")
        integer(state["consumed"], 0, "consumed samples")
        if state["consumed"] > self.budget:
            raise ValueError("restored consumption exceeds budget")
        for field in ("counts", "order", "cursor"):
            exact_fields(state[field], self.matches, "sampler " + field)
        total = 0
        for match_id in self.matches:
            episode, _ = self.store.load(match_id)
            if episode.sha256 != self.entries[match_id]["sha"]:
                raise ValueError("replay data changed since checkpoint")
            counts, order, cursor = (state[field][match_id] for field in ("counts", "order", "cursor"))
            size = self.entries[match_id]["rows"]
            if len(counts) != size or len(order) != size or any(type(value) is not int for value in order):
                raise ValueError("restored row coverage differs")
            for value in counts:
                integer(value, 0, "row coverage")
            integer(cursor, 0, "row cursor")
            if sorted(order) != list(range(size)) or cursor > size or max(counts) - min(counts) > 1:
                raise ValueError("restored row permutation or balanced coverage differs")
            cycles, remainder = divmod(sum(counts), size)
            if cursor not in ([0, size] if remainder == 0 else [remainder]):
                raise ValueError("row cursor disagrees with coverage")
            if cursor == 0 and sum(counts) != 0:
                raise ValueError("nonempty coverage cannot reset its cursor")
            if remainder and any(counts[index] != cycles + int(position < cursor) for position, index in enumerate(order)):
                raise ValueError("row ordering disagrees with coverage")
            total += sum(counts)
        if total != state["consumed"]:
            raise ValueError("sample counter differs from coverage")
        totals = [sum(values) for values in state["counts"].values()]
        if max(totals) - min(totals) > 1:
            raise ValueError("whole-episode sampling balance differs")
        restored_rng = random.Random()
        restored_rng.setstate(state["rng"])
        for field in ("counts", "order", "cursor", "consumed"):
            setattr(self, field, deepcopy(state[field]))
        self.rng = restored_rng
