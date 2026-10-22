"""Resumable multi-deck evaluation on the same fleet, between PPO rounds."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import shlex

from native_runner.training.event_history_assets import probe_artifacts, artifact_path
from native_runner.training.event_opponents import digest
from native_runner.training.mixed_evaluation import (
    specs_for, matrix_cells, comparison, protection, validate_spec,
)
from tools.event_rl_host import read_reply


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


class MixedEvaluator:
    def __init__(self, collector, contract_sha):
        self.collector, self.fleet = collector, collector.fleet
        self.contract_sha = contract_sha
        self.config = self.fleet.CFG["mixed_opponents"]
        self.baseline_sha = self.config["baseline"]["sha256"]
        self.directory = self.fleet.ROOT / "evaluations"
        self.directory.mkdir(exist_ok=True)
        self.runtime_sha = hashlib.sha256((self.fleet.ROOT / self.fleet.CFG["collector_manifest"]).read_bytes()).hexdigest()
        self.descriptors = {}

    def specs(self, sha, cells, pairs, suite):
        return specs_for(sha, cells, pairs, suite=suite, runtime_sha=self.runtime_sha,
                         action_constraints=self.config["action_constraints"], seed_base=self.fleet.CFG["seed_base"])

    def assets(self, sources):
        f, c = self.fleet, self.collector
        missing = sorted(set(sources) - set(self.descriptors))
        if missing:
            response = c.history_request("37", dict(operation="archive", root=f.FLEET["37"]["run"],
                sources=missing, contract_sha=self.contract_sha))
            descriptors = response["descriptors"]
            if set(descriptors) != set(missing):
                raise ValueError("evaluation archive changed weights")
            local = probe_artifacts(f.ROOT, descriptors, self.contract_sha)
            if local["missing"]:
                c.transfer_histories("37", descriptors, local["missing"], pull=True)
            probe_artifacts(f.ROOT, descriptors, self.contract_sha, require_complete=True)
            self.descriptors.update({sha: dict(d, path=str(artifact_path(f.ROOT, d))) for sha, d in descriptors.items()})
        return {sha: self.descriptors[sha] for sha in sources}

    def run_host(self, host, jobs, descriptors):
        f, c = self.fleet, self.collector
        remote_root = Path(f.FLEET[host]["run"])
        response = c.history_request(host, dict(operation="probe", root=str(remote_root),
            descriptors=descriptors, contract_sha=self.contract_sha))
        if response["missing"]:
            c.transfer_histories(host, descriptors, response["missing"])
        remote = {sha: dict(d, path=str(remote_root / "history-inference" / Path(d["path"]).name))
                  for sha, d in descriptors.items()}
        process = c.processes.get(host) or c.start(host)
        if process.poll() is not None:
            raise RuntimeError("evaluation host exited")
        wave_id = digest([j["spec"]["sha256"] for j in jobs])
        process.stdin.write(json.dumps(dict(operation="evaluate", jobs=jobs,
                                           descriptors=remote, wave_id=wave_id)) + "\n")
        process.stdin.flush()
        reply = read_reply(process, 6100)
        if not reply.get("success") or reply.get("wave_id") != wave_id:
            raise RuntimeError(f"evaluation wave rejected: {reply}")
        by_port = {j["port"]: j["spec"] for j in jobs}
        if {r["port"] for r in reply["results"]} != set(by_port):
            raise ValueError("evaluation reply ports mismatch")
        for result in reply["results"]:
            record = result["evaluation"]
            if record["spec"] != by_port[result["port"]] or not record["true_terminal"]:
                raise ValueError("evaluation reply identity or terminal mismatch")
            write_json(self.directory / "results" / (record["spec"]["sha256"] + ".json"), record)
        return len(reply["results"])

    def games(self, specs):
        unique = {validate_spec(s)["sha256"]: s for s in specs}
        pending = []
        for sha, spec in unique.items():
            path = self.directory / "results" / (sha + ".json")
            if path.exists():
                record = json.loads(path.read_text())
                if record["spec"] != spec or not record.get("true_terminal"):
                    raise ValueError("persisted evaluation identity mismatch")
            else:
                pending.append(spec)
        slots = [(h, p) for h, config in self.fleet.FLEET.items() for p in config["ports"]]
        self.fleet.event("evaluation-start", total_games=len(unique), remaining_games=len(pending),
                         suite=next(iter(unique.values()))["suite"])
        for offset in range(0, len(pending), len(slots)):
            self.fleet.stop_check("evaluation-wave-boundary")
            by_host = {}
            for (h, port), spec in zip(slots, pending[offset:offset + len(slots)]):
                by_host.setdefault(h, []).append(dict(port=port, spec=spec))
            sources = {s[k] for jobs in by_host.values() for j in jobs for s in [j["spec"]]
                       for k in ("candidate_sha256", "opponent_sha256")}
            descriptors = self.assets(sources)
            with concurrent.futures.ThreadPoolExecutor(len(by_host)) as pool:
                futures = [pool.submit(self.run_host, h, jobs, descriptors) for h, jobs in by_host.items()]
                for future in futures:
                    future.result()
            self.fleet.event("evaluation-progress", completed=min(offset + len(slots), len(pending)),
                             newly_required_games=len(pending), suite=pending[offset]["suite"])
        return [json.loads((self.directory / "results" / (s["sha256"] + ".json")).read_text()) for s in specs]

    def smoke_and_baseline(self):
        cells = matrix_cells(self.baseline_sha)
        smoke = [s for s in self.specs(self.baseline_sha, cells, 1, "smoke") if s["owner"] == 0]
        records = self.games(smoke)
        if any(r["counters"]["act_violation"] or not r["execution_status_counts"].get("executed", 0)
               for r in records):
            raise ValueError("native smoke lacks legal executed actions")
        write_json(self.directory / "smoke.json", dict(passed=True, games=16,
            baseline_sha256=self.baseline_sha, runtime_sha256=self.runtime_sha,
            records=[r["spec"]["sha256"] for r in records]))
        records = self.games(self.specs(self.baseline_sha,
            matrix_cells(self.baseline_sha, self.config["fixed"]), 10, "protection"))
        report = comparison(records, records, self.baseline_sha)
        write_json(self.directory / "baseline.json", report)
        return report

    def evaluate(self, sha, round_number):
        path = self.directory / f"round-{round_number:03d}.json"
        if path.exists():
            previous = json.loads(path.read_text())
            if previous["checkpoint_sha256"] != sha or previous["runtime_sha256"] != self.runtime_sha:
                raise ValueError("saved round evaluation identity mismatch")
            return previous
        all_cells = matrix_cells(self.baseline_sha, self.config["fixed"])
        golem_cells = [cell for cell in all_cells if cell[0] == "golem"]
        other_cells = [cell for cell in all_cells if cell[0] != "golem"]
        boundary = round_number in (20, 60, 100)
        schedule = (10, 20, 40, 80, 120, 200)
        expansion = []
        for pairs in schedule:
            candidate_specs = self.specs(sha, other_cells, 10, "protection") + self.specs(sha, golem_cells, pairs, "protection")
            baseline_specs = self.specs(self.baseline_sha, other_cells, 10, "protection") + self.specs(self.baseline_sha, golem_cells, pairs, "protection")
            records = self.games(candidate_specs + baseline_specs)
            n = len(candidate_specs)
            report = comparison(records[:n], records[n:], self.baseline_sha)
            guard = protection(report, stage_boundary=boundary, at_limit=pairs == 200)
            expansion.append(dict(pairs_per_golem_cell=pairs, protection=guard))
            if not guard["expand"]:
                break
        result = dict(checkpoint_sha256=sha, mixed_round=round_number, runtime_sha256=self.runtime_sha,
                      report=report, protection=guard, expansion=expansion)
        write_json(path, result)
        return result

    def compare_independent(self, sha, *, variants=False):
        cells = matrix_cells(self.baseline_sha, variants=variants)
        suite = "heldout" if variants else "independent-selection"
        specs = self.specs(sha, cells, 25, suite)
        base = self.specs(self.baseline_sha, cells, 25, suite)
        records = self.games(specs + base)
        report = comparison(records[:len(specs)], records[len(specs):], self.baseline_sha)
        report["checkpoint_sha256"] = sha
        report["improvement_status"] = ("verified" if report["equal_deck_average"]["delta_ci95"][0] > 0
                                        else "not_yet_verified")
        write_json(self.directory / (suite + ".json"), report)
        return report

    def final_head_to_head(self, final_sha, opponents):
        from native_runner.training.mixed_decks import DECK_IDS
        results = {}
        for sha in dict.fromkeys(opponents):
            if sha == final_sha:
                continue
            cells = [(d, d, sha) for d in DECK_IDS]
            specs = self.specs(final_sha, cells, 25, "final-head-to-head")
            records = self.games(specs)
            # Self-comparison here supplies per-cell scores/CIs without claiming an uplift baseline.
            report = comparison(records, records, sha)
            results[sha] = dict(cells={k: {field: value for field, value in v.items()
                if field not in ("delta", "delta_ci95", "baseline_score_rate")}
                for k, v in report["cells"].items()})
        write_json(self.directory / "final-head-to-head.json", results)
        return results
