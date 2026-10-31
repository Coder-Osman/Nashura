"""Validate independent lifecycle diagnostics; never publish their timing."""
import hashlib
import re

from summarize_headless_benchmark import records
from summarize_headless_stability import summarize as validate_stability

PHASES = ("pre_create", "pre_step", "post_step", "post_terminal", "post_destroy")
GROUPS = ("art_named", "allocator_named", "stack_named", "anonymous_other",
          "file_named", "special_other")
HEAP_KEYS = ("arena_bytes", "hblkhd_bytes", "uordblks_bytes", "fordblks_bytes")
MAP_KEYS = ("maps", "size_kb", "rss_kb", "pss_kb", "anonymous_kb", "private_dirty_kb")


def fields(line, marker, keys):
    tokens = line.split()[1:]
    if any(token.count("=") != 1 for token in tokens):
        raise ValueError("malformed memory diagnostic fields")
    row = dict(token.split("=", 1) for token in tokens)
    if len(row) != len(tokens) or set(row) != set(keys):
        raise ValueError("missing, duplicate or unknown memory diagnostic fields")
    if not line.startswith(marker + " ") or row["scope"] != "diagnostic-only":
        raise ValueError("memory diagnostic scope required")
    return row


def natural(row, key):
    if not re.fullmatch(r"[0-9]+", row[key]):
        raise ValueError("nonnegative memory integer required: " + key)
    return int(row[key])


def summarize(text, batches, repeats, expected_tick, expected_digest, expected_bytes, interval):
    if type(interval) is not int or not 1 <= interval <= 1000000:
        raise ValueError("invalid memory diagnostic interval")
    lines = text.splitlines()
    config = ("HEADLESS_MEMORY_LIFECYCLE_CONFIG scope=diagnostic-only interval="
              + str(interval) + " allocator_api=mallinfo allocator_scope=process-global classification=pathname-heuristic")
    configs = [i for i, line in enumerate(lines) if line.startswith("HEADLESS_MEMORY_LIFECYCLE_CONFIG")]
    if len(configs) != 1 or lines[configs[0]] != config:
        raise ValueError("unique exact diagnostic configuration required")
    # Reuse the existing complete terminal/task contract, discarding every
    # derived timing statistic. Removing diagnostics here does not certify timing.
    plain = chr(10).join(line for line in lines if not line.startswith(
        ("HEADLESS_MEMORY_LIFECYCLE", "HEADLESS_MEMORY_MAPPING")))
    validated = validate_stability(plain, batches, repeats, expected_tick,
                                   expected_digest, expected_bytes, 0)
    metrics = records(text, "HEADLESS_BATTLE_METRICS")
    metric_positions = [i for i, line in enumerate(lines) if line.startswith("HEADLESS_BATTLE_METRICS ")]
    reset_positions = [i for i, line in enumerate(lines) if line.startswith("HEADLESS_BATTLE_RESET ")]
    positions = [i for i, line in enumerate(lines) if line.startswith(
        ("HEADLESS_MEMORY_LIFECYCLE ", "HEADLESS_MEMORY_MAPPING "))]
    if any(line.startswith(("HEADLESS_MEMORY_LIFECYCLE", "HEADLESS_MEMORY_MAPPING"))
           and i not in positions and i not in configs for i, line in enumerate(lines)):
        raise ValueError("unknown memory diagnostic record")
    expected_sequences = list(range(0, batches * repeats, interval))
    if len(positions) != len(expected_sequences) * len(PHASES) * (1 + len(GROUPS)):
        raise ValueError("incomplete or duplicate lifecycle samples")
    samples, cursor, previous_end = [], 0, -1
    for sequence in expected_sequences:
        phases, at = {}, {}
        for phase in PHASES:
            position = positions[cursor]
            cursor += 1
            row = fields(lines[position], "HEADLESS_MEMORY_LIFECYCLE",
                ("scope", "sequence", "phase", "start_ns", "end_ns") + HEAP_KEYS)
            if natural(row, "sequence") != sequence or row["phase"] != phase:
                raise ValueError("wrong lifecycle sample order")
            start, end = natural(row, "start_ns"), natural(row, "end_ns")
            if start <= max(0, previous_end) or end < start:
                raise ValueError("overlapping lifecycle observations")
            previous_end = end
            mappings = {}
            for group in GROUPS:
                map_position = positions[cursor]
                cursor += 1
                if map_position != position + 1 + len(mappings):
                    raise ValueError("lifecycle mapping records must be contiguous")
                mapping = fields(lines[map_position], "HEADLESS_MEMORY_MAPPING",
                    ("scope", "sequence", "phase", "group") + MAP_KEYS)
                if natural(mapping, "sequence") != sequence or mapping["phase"] != phase or mapping["group"] != group:
                    raise ValueError("wrong lifecycle mapping order")
                values = {key: natural(mapping, key) for key in MAP_KEYS}
                if not (values["pss_kb"] <= values["rss_kb"] <= values["size_kb"]
                        and values["anonymous_kb"] <= values["rss_kb"]
                        and values["private_dirty_kb"] <= values["rss_kb"]):
                    raise ValueError("inconsistent mapping accounting")
                mappings[group] = values
            if not sum(value["maps"] for value in mappings.values()):
                raise ValueError("empty smaps classification")
            phases[phase] = dict(heap={key: natural(row, key) for key in HEAP_KEYS},
                                mappings=mappings, start_ns=start, end_ns=end)
            at[phase] = position
        if (not (configs[0] < at["pre_create"] < at["pre_step"] < at["post_step"]
                 < metric_positions[sequence] < at["post_terminal"]
                 < at["post_destroy"] < reset_positions[sequence])
                or (sequence and at["pre_create"] <= reset_positions[sequence - 1])
                or phases["pre_step"]["end_ns"] > int(metrics[sequence]["start_ns"])
                or phases["post_step"]["start_ns"] < int(metrics[sequence]["end_ns"])):
            raise ValueError("lifecycle samples outside required match boundaries")
        samples.append(dict(sequence=sequence, phases=phases))
    trends = {}
    for phase in PHASES:
        selected = [sample["phases"][phase] for sample in samples]
        def span(values):
            return dict(first=values[0], last=values[-1], min=min(values), max=max(values),
                        last_minus_first=values[-1] - values[0])
        trends[phase] = dict(heap_bytes={key: span([row["heap"][key] for row in selected])
            for key in HEAP_KEYS}, mappings={group: {key: span([row["mappings"][group][key]
                for row in selected]) for key in MAP_KEYS} for group in GROUPS})
    return dict(schema="nashura.headless-memory-lifecycle.v1", scope="diagnostic-only",
        benchmark=False, whole_goal_complete=False, matches=validated["matches"],
        interval=interval, sampled_matches=len(samples), phases=list(PHASES),
        trends=trends, samples=samples, stdout_sha256=hashlib.sha256(text.encode()).hexdigest(),
        limitations=["process-global allocator snapshots are allocator-specific, not allocation counts or live object identities",
            "smaps groups use pathname heuristics; ART-named mappings are not the complete Java heap",
            "smaps is sampled sequentially while other runtime threads may execute",
            "diagnostic sampling perturbs residency and allocator state; no benchmark inference",
            "net memory growth or plateau alone does not prove a leak or absence of leaks",
            "terminal validation is separate from full-frame correctness gates"])
