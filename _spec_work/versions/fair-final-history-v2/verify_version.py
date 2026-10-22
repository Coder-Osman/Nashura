import hashlib
import importlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as element_tree


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_report(path, expected_count, allowed_skips):
    tree = element_tree.parse(path)
    cases = list(tree.iter("testcase"))
    if len(cases) != expected_count:
        raise ValueError(f"unexpected test count: {path}")
    if any(list(tree.iter(tag)) for tag in ("error", "failure")):
        raise ValueError(f"test failure: {path}")
    skipped = []
    for case in cases:
        exclusion = case.find("skipped")
        if exclusion is not None:
            skipped.append(dict(name=case.attrib["name"], reason=exclusion.attrib.get("message", "")))
    if {item["name"] for item in skipped} != set(allowed_skips):
        raise ValueError(f"unexpected test exclusions: {path}")
    if any(item["reason"] != "fixed native artifact not installed" for item in skipped):
        raise ValueError(f"unexpected exclusion reason: {path}")
    return dict(path=path.name, sha256=digest(path), tests=len(cases),
        passed=len(cases) - len(skipped), failures=0, errors=0, skipped=skipped)


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    project = root / "project"
    baseline = json.loads((root / "base-source-manifest.json").read_text())
    changed = sorted(name for name, expected in baseline.items() if digest(project / name) != expected)
    production_changes = sorted(f"native_runner/training/fair_final/{name}.py"
        for name in ("history", "history_belief", "native", "prior_history", "conditioned_plan", "refill_belief"))
    test_changes = [f"native_runner/tests/test_fair_final_{name}.py" for name in ("history", "conditioned_plan")]
    if len(baseline) != 460 or changed != sorted(production_changes + test_changes):
        raise ValueError("unexpected source changes")
    for manifest_name, prefix, count in (
        ("tooling-source-manifest.json", "tools", 86),
        ("resource-source-manifest.json", "native_runner/data", 12),
    ):
        manifest = json.loads((root / manifest_name).read_text())
        if len(manifest["files"]) != count or any(
            digest(project / prefix / name) != expected for name, expected in manifest["files"].items()
        ):
            raise ValueError(f"source dependency mismatch: {manifest_name}")
    fixtures = ["artifacts/standalone-resident-20260927/build/phase_runtime_telemetry.inc",
        "native_runner/probe/test_fair_final_phase_snapshot.cpp",
        "native_runner/probe/phase_runtime_layout.h",
        "native_runner/probe/fair_final_phase_observation.h",
        "native_runner/probe/fair_final_phase_snapshot.inc"]
    fixture_hashes = {name: digest(project / name) for name in fixtures}
    if any(digest(workspace / "firstlight_CR" / name) != expected
        for name, expected in fixture_hashes.items()):
        raise ValueError("native test fixture source mismatch")
    launch_path = workspace / "_spec_work/evidence/fair-final-v1-20261019" / (
        "native-search-episode-1792552718166807000/launch.json")
    launch = json.loads(launch_path.read_text())
    frozen = launch["source_sha256"]
    if len(frozen) != 244 or any(digest(workspace / "firstlight_CR" / name) != expected
        for name, expected in frozen.items()):
        raise ValueError("running attempt source identity changed")
    sys.path.insert(0, str(project))
    imported = {}
    for name in ("history", "history_belief", "native", "prior_history", "conditioned_plan", "refill_belief"):
        module = importlib.import_module(f"native_runner.training.fair_final.{name}")
        path = Path(module.__file__).resolve()
        if path != project / f"native_runner/training/fair_final/{name}.py":
            raise ValueError("import escaped isolated version")
        imported[name] = str(path)
    reports = [test_report(root / "proposal-tests.xml", 88, ()),
        test_report(root / "regression-full-support.xml", 649, (
            "test_target_type_static_evidence_is_bound_to_frozen_library",
            "test_frozen_native_inventory_anchors_and_call_counts"))]
    files = {str(path.relative_to(project)): digest(path) for path in sorted(project.rglob("*"))
        if path.is_file() and not any(part in ("__pycache__", ".pytest_cache") for part in path.parts)}
    if any(path.is_symlink() for path in project.rglob("*")):
        raise ValueError("isolated version contains symlink")
    provenance = {name: digest(root / name) for name in (
        "base-source-manifest.json", "tooling-source-manifest.json", "resource-source-manifest.json",
        "CONTRACT.md", "verify_version.py", "version-manifest-before-full-support.json",
        "verification-before-full-support.json")}
    version = dict(schema="fair-final-history-version.v2", history_schema="fair-final-visible-history.v2",
        hidden_plan_proposal=importlib.import_module("native_runner.training.fair_final.history_belief").HIDDEN_PLAN_PROPOSAL,
        source_files=files, provenance_sha256=provenance, native_test_fixture_sha256=fixture_hashes,
        modified_production_files=production_changes, modified_existing_tests=test_changes, training_admissible=False)
    manifest_path = root / "version-manifest.json"
    manifest_path.write_text(json.dumps(version, indent=2, ensure_ascii=False) + "\n")
    verification = dict(schema="fair-final-history-verification.v2",
        status="engineering_checks_passed_with_explicit_native_exclusions",
        version_manifest_sha256=digest(manifest_path), tests=reports, imports=imported,
        running_attempt=dict(launch_sha256=digest(launch_path), frozen_files=len(frozen), changed_files=0),
        native=False, native_reachability_proven=False, general_pending_closed=False,
        training_admissible=False, deployed=False,
        opponent_private_rejection_cold_support="fixture_verified_approximate_full_policy_mixture",
        remaining_gaps=["legal-native-rejection-reachability", "native-v2-replay-full128-and-recovery",
            "complete-search-episode-and-independent-final-verification"],
        test_counts_overlap=True, initial_regression_failure="missing copied C++ fixture; retained in regression-tests.xml")
    (root / "verification.json").write_text(json.dumps(verification, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(dict(status=verification["status"], source_files=len(files),
        modified_production_files=production_changes, tests=reports, frozen_files=len(frozen),
        changed_frozen_files=0, training_admissible=False), ensure_ascii=False))


if __name__ == "__main__":
    main()
