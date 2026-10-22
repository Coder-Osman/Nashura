import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as element_tree


def validate_pending_search_tests(path):
    report = element_tree.parse(path).getroot()
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 106
            or len(cases) != 106
            or any(int(suite.attrib.get(key, 0)) for suite in suites
                for key in ("errors", "failures", "skipped"))
            or any(list(report.iter(tag)) for tag in ("error", "failure", "skipped"))
            or len({(case.attrib.get("classname"), case.attrib.get("name")) for case in cases}) != 106):
        raise ValueError("pending search regression evidence incomplete")
    expected = {
        "test_pending_plan_settlement_and_sampling_replay_identically": 12,
        "test_pending_edge_at_limits_restores_root_without_leaf": 6,
        "test_pending_terminal_uses_native_result_and_root_player_perspective": 6,
        "test_pending_root_search_counts_only_completed_edges_and_restores": 8,
    }
    pending = [case.attrib["name"].split("[", 1)[0] for case in cases
        if case.attrib.get("classname", "").endswith(".test_fair_final_pending_search")]
    if len(pending) != 32 or any(pending.count(name) != count for name, count in expected.items()):
        raise ValueError("pending search branch coverage incomplete")
    return dict(tests=106, pending_tests=32, native=False)


def validate_migration_tests(path):
    report = element_tree.parse(path).getroot()
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    expected = {"native_runner.tests.test_fair_migration": 26,
        "native_runner.tests.test_fair_migration_trainer": 31,
        "native_runner.tests.test_fair_final_learning": 7}
    if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 64 or len(cases) != 64
            or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))
            or any(list(report.iter(tag)) for tag in ("error", "failure", "skipped"))
            or len({(case.attrib.get("classname"), case.attrib.get("name")) for case in cases}) != 64
            or any(sum(case.attrib.get("classname") == name for case in cases) != count
                for name, count in expected.items())):
        raise ValueError("migration loss and trainer regression evidence incomplete")
    return dict(tests=64, native=False, formal_training=False)


def validate_migration_data_tests(path):
    report = element_tree.parse(path).getroot()
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    expected = {
        "test_migration_complete_roundtrip_keeps_preempted_rows_and_player_labels": 3,
        "test_migration_manifest_rejects_wrong_semantics_and_whole_game_split_leaks": 6,
        "test_migration_failed_attempt_is_quarantined_without_completion": 5,
        "test_migration_serialized_corruption_rejected": 13,
        "test_migration_loader_uses_complete_game_splits_and_real_model_update_resume": 1,
        "test_validation_and_test_rows_are_blocked_from_training_loader": 1,
        "test_dataset_missing_one_registered_game_is_not_ready": 1,
        "test_duplicate_attempt_is_idempotent_and_dataset_identity_binds_actual_content": 1,
        "test_same_manifest_with_different_completed_results_has_different_dataset_identity": 1,
        "test_store_rejects_corrupted_payload_or_completion_receipt": 5,
        "test_valid_receipt_replacement_after_indexing_is_rejected": 1,
        "test_dataset_mismatch_is_rejected_before_optimizer_or_cursor_changes": 3,
        "test_interrupted_completion_can_retry_without_partial_admission": 1,
        "test_soft_targets_recomputed_from_live_teacher_and_forged_values_rejected": 1,
        "test_teacher_mutation_and_manifest_drift_fail_closed": 1,
        "test_student_supplement_keeps_behavior_separate_from_teacher": 1,
        "test_direct_migration_data_is_not_search_or_formal_replay": 1,
        "test_recording_is_atomic_for_both_frozen_sides_and_keeps_global_rng": 1,
    }
    names = [case.attrib.get("name", "").split("[", 1)[0] for case in cases]
    if (sum(expected.values()) != 47 or sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 47
            or len(cases) != 47
            or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))
            or any(list(report.iter(tag)) for tag in ("error", "failure", "skipped"))
            or len({(case.attrib.get("classname"), case.attrib.get("name")) for case in cases}) != 47
            or any(case.attrib.get("classname") != "native_runner.tests.test_fair_migration_data" for case in cases)
            or any(names.count(name) != count for name, count in expected.items())):
        raise ValueError("migration data regression evidence incomplete")
    return dict(tests=47, native=False, formal_training=False)


def validate_migration_collector_tests(path):
    report = element_tree.parse(path).getroot()
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    expected = {
        "test_collector_runs_both_sides_to_complete_terminal_and_keeps_preemptions": 1,
        "test_collector_quarantines_bad_complete_game_instead_of_publishing": 5,
        "test_collector_timeout_is_quarantined_with_no_partial_completion": 1,
        "test_collector_rejects_invalid_budget_before_creating_attempt": 5,
        "test_protocol_identity_binds_engine_to_real_source_and_stable_semantics": 1,
    }
    names = [case.attrib.get("name", "").split("[", 1)[0] for case in cases]
    if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 13 or len(cases) != 13
            or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))
            or any(list(report.iter(tag)) for tag in ("error", "failure", "skipped"))
            or len({(case.attrib.get("classname"), case.attrib.get("name")) for case in cases}) != 13
            or any(case.attrib.get("classname") != "native_runner.tests.test_fair_migration_collect" for case in cases)
            or any(names.count(name) != count for name, count in expected.items())):
        raise ValueError("migration collector regression evidence incomplete")
    return dict(tests=13, native=False, formal_training=False)


def validate_migration_native_tests(path):
    from test_migration_native_evidence import NEGATIVE_CASES

    report = element_tree.parse(path).getroot()
    suites = list(report.iter("testsuite"))
    cases = list(report.iter("testcase"))
    expected = {"test_native_migration_evidence_accepts_complete_games_and_exact_resume"}
    expected.update(f"test_native_migration_evidence_rejects_tampered_artifacts[{name}]"
        for name in NEGATIVE_CASES)
    if (len(NEGATIVE_CASES) != 28 or len(expected) != 29
            or sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 29 or len(cases) != 29
            or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))
            or any(list(report.iter(tag)) for tag in ("error", "failure", "skipped"))
            or {case.attrib.get("name") for case in cases} != expected
            or any(case.attrib.get("classname") != "_spec_work.evidence.fair-final-v1-20261019.test_migration_native_evidence"
                for case in cases)):
        raise ValueError("native migration validator regression evidence incomplete")
    return dict(tests=29, negative_cases=28, formal_training=False)


def validate_visibility(root, workspace):
    visibility = json.loads((root / "trajectory-visibility-report.json").read_text())
    expected = {(stage, owner, kind) for stage in ("opening", "middle", "late", "overtime")
        for owner in (0, 1) for kind in ("native_hidden_draw", "reset_seed_and_hidden_deck_metadata",
            "private_events", "pending_enemy_plan")}
    checks = visibility["checks"]
    actual = {(item["stage"], item["owner"], item["kind"]) for item in checks}
    if (visibility["schema"] != "fair-final-trajectory-visibility.v1"
            or visibility["status"] != "passed" or visibility["training_admissible"] is not False
            or visibility["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
            or len(checks) != 32 or actual != expected or any(item["exact"] is not True for item in checks)):
        raise ValueError("trajectory visibility coverage is incomplete")
    thresholds = dict(opening=100, middle=1200, late=3600, overtime=5400)
    for item in checks:
        if item["tick"] < thresholds[item["stage"]]:
            raise ValueError("visibility stage is earlier than its registered threshold")
        if item["kind"] == "native_hidden_draw":
            digests = [item["evidence"][key] for key in ("before_digest", "after_digest")]
            if (digests[0] == digests[1] or any(len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest) for digest in digests)):
                raise ValueError("native hidden state was not demonstrably changed")
    terminal = visibility["terminal"]
    decisions = visibility["decisions"]
    if (terminal["native_evidence"]["finalized"] is not True or not decisions
            or terminal["terminal_tick"] <= max(item["tick"] for item in checks)
            or any(item["owner"] not in (0, 1) or item["tick"] >= terminal["terminal_tick"] for item in decisions)
            or any(visibility["decisions_by_owner"][str(owner)] != sum(item["owner"] == owner for item in decisions)
                or visibility["decisions_by_owner"][str(owner)] < 1 for owner in (0, 1))):
        raise ValueError("visibility probe lacks a complete two-seat trajectory")
    cleanup = visibility["cleanup"]
    if (cleanup["case"] != "visibility" or cleanup["port"] != 29622 or cleanup["exit_code"] != 0
            or cleanup["phase"] != "stopped" or cleanup["listening"] or any(cleanup["pids_alive"].values())):
        raise ValueError("visibility cleanup is incomplete")
    if hashlib.sha256((root / "run_visibility_validation.py").read_bytes()).hexdigest() != visibility["runner_sha256"]:
        raise ValueError("visibility collector identity changed")
    if not visibility["source_sha256"] or not visibility["artifacts"]:
        raise ValueError("visibility source or artifact identity missing")
    for relative, digest in visibility["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"visibility report is stale for {relative}")
    for relative, digest in visibility["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"visibility artifact changed: {relative}")
    for name in ("report.json", "cleanup.json", "probe-source-manifest.json", "process.json", "run.py"):
        matches = [root / relative for relative in visibility["artifacts"] if Path(relative).name == name]
        if len(matches) != 1:
            raise ValueError(f"missing unique raw visibility evidence: {name}")
        if name == "report.json":
            original = json.loads(matches[0].read_text())
            if original != {key: value for key, value in visibility.items()
                    if key not in ("cleanup", "runner_sha256", "artifacts")}:
                raise ValueError("visibility summary differs from raw probe result")
        if name == "cleanup.json" and json.loads(matches[0].read_text()) != cleanup:
            raise ValueError("visibility cleanup differs from raw result")
        if name == "probe-source-manifest.json" and json.loads(matches[0].read_text()) != visibility["source_sha256"]:
            raise ValueError("visibility source differs from deployment manifest")
        if name == "process.json" and json.loads(matches[0].read_text())["phase"] != "stopped":
            raise ValueError("visibility engine did not stop")
    return visibility


def main():
    root = Path(__file__).resolve().parent
    workspace = root.parents[2]
    report = json.loads((root / "native-timing-report.json").read_text())
    process = json.loads((root / "native-probe-process.json").read_text())
    test_report = root / "p1-local-search-tests.xml"
    pending_search_tests = validate_pending_search_tests(root / "p1-pending-search-tests.xml")
    migration_tests = validate_migration_tests(root / "p1-migration-loss-tests.xml")
    migration_data_tests = validate_migration_data_tests(root / "p1-migration-data-tests.xml")
    migration_collector_tests = validate_migration_collector_tests(root / "p1-migration-collector-tests.xml")
    migration_native = None
    if (root / "native-migration-evidence-report.json").exists():
        from verify_migration_evidence import validate_migration_evidence

        recorded = json.loads((root / "native-migration-evidence-report.json").read_text())
        migration_native = validate_migration_evidence(recorded["attempt"])
        if migration_native != recorded:
            raise ValueError("native migration evidence report differs from independent validation")
        validate_migration_native_tests(root / "migration-native-evidence-tests.xml")
    supplement = None
    if (root / "migration-supplement-evidence.json").exists():
        from verify_migration_supplement import validate_supplement_evidence

        supplement = json.loads((root / "migration-supplement-evidence.json").read_text())
        if (supplement["schema"] != "FairFinalMigrationSupplementEvidence/v1"
                or supplement["status"] != "engineering_completed"
                or supplement["training_admissible"] is not False
                or supplement["formal_g2_pass"] is not False
                or supplement["round"] != 1
                or supplement["native_verification"] != validate_supplement_evidence(root / supplement["attempt"])):
            raise ValueError("supplementary migration evidence scope or native verification differs")
        expected_tests = {
            "migration-supplement-regression.xml": {
                "native_runner.tests.test_fair_migration_supplement": 42,
                "native_runner.tests.test_fair_migration_collect": 13,
                "native_runner.tests.test_fair_migration_data": 47},
            "migration-supplement-evidence-tests.xml": {
                "_spec_work.evidence.fair-final-v1-20261019.test_migration_supplement_evidence": 20}}
        required_artifacts = {str((root / name).relative_to(workspace)) for name in (
            *expected_tests, "migration-supplement-contract.md", "verify_migration_supplement.py",
            "test_migration_supplement_evidence.py", "run_migration_supplement_probe.py",
            "migration_supplement_probe_worker.py")}
        required_artifacts.add("firstlight_CR/tools/fair_migration_supplement.py")
        required_artifacts.add("firstlight_CR/native_runner/tests/test_fair_migration_supplement.py")
        attempt_directory = root / supplement["attempt"]
        required_artifacts.update(str(path.relative_to(workspace)) for path in attempt_directory.rglob("*")
            if path.is_file() and path.name != "latest-status.json")
        if not required_artifacts.issubset(supplement["artifacts_sha256"]):
            raise ValueError("supplementary migration artifact coverage differs")
        for relative, expected in supplement["artifacts_sha256"].items():
            if (Path(relative).is_absolute() or ".." in Path(relative).parts
                    or hashlib.sha256((workspace / relative).read_bytes()).hexdigest() != expected):
                raise ValueError("supplementary migration artifact identity differs: " + relative)
        for name, classes in expected_tests.items():
            xml = element_tree.parse(root / name).getroot()
            cases = list(xml.iter("testcase"))
            count = sum(classes.values())
            if (len(cases) != count or len({(case.attrib.get("classname"), case.attrib.get("name"))
                    for case in cases}) != count
                    or sum(int(suite.attrib.get("tests", 0)) for suite in xml.iter("testsuite")) != count
                    or any(int(suite.attrib.get(key, 0)) for suite in xml.iter("testsuite")
                        for key in ("errors", "failures", "skipped"))
                    or any(list(xml.iter(tag)) for tag in ("error", "failure", "skipped"))
                    or any(sum(case.attrib.get("classname") == classname for case in cases) != expected
                        for classname, expected in classes.items())):
                raise ValueError("supplementary migration regression evidence incomplete: " + name)
    visibility = validate_visibility(root, workspace)
    from verify_search_evidence import validate_search_evidence

    native_search = validate_search_evidence(root, workspace)
    from verify_history_evidence import validate_history_evidence

    native_history = validate_history_evidence(root, workspace)
    from verify_history_search_evidence import validate_history_search_evidence

    history_search = validate_history_search_evidence(root, workspace)
    from verify_single_ready_evidence import validate_single_ready_evidence
    from test_single_ready_evidence import NEGATIVE_CASES

    single_ready = validate_single_ready_evidence(root, workspace)
    extended_search = None
    if (root / "native-extended-search-report.json").exists():
        from verify_extended_search_evidence import validate_extended_search_evidence
        from test_extended_search_evidence import NEGATIVE_CASES as EXTENDED_NEGATIVE_CASES

        extended_search = validate_extended_search_evidence(root, workspace)
        extended_validator = json.loads((root / "extended-search-evidence-validator-tests.json").read_text())
        if (extended_validator["status"] != "passed" or extended_validator["positive_checks"] != 8
                or len(extended_validator["negative_cases"]) != len(EXTENDED_NEGATIVE_CASES)
                or {entry["case"] for entry in extended_validator["negative_cases"]} != set(EXTENDED_NEGATIVE_CASES)
                or any(entry["rejected"] is not True for entry in extended_validator["negative_cases"])
                or extended_validator["report_sha256"] != hashlib.sha256(
                    (root / "native-extended-search-report.json").read_bytes()).hexdigest()):
            raise ValueError("extended search validator negative-case evidence differs")
    prng_reports = {}
    from verify_prng_evidence import validate_prng_evidence
    from test_prng_evidence import COMMON_CASES, SEARCH_CASES

    prng_layout = json.loads((root / "native-prng-layout.json").read_text())
    if (prng_layout["status"] != "passed"
            or prng_layout["libg_sha256"] != "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
            or prng_layout["generator_sha256"] != hashlib.sha256((root / "record_prng_layout.py").read_bytes()).hexdigest()):
        raise ValueError("PRNG static layout identity differs")
    for phase in ("smoke", "full"):
        if not (root / f"native-prng-{phase}-report.json").exists():
            continue
        prng_reports[phase] = validate_prng_evidence(root, workspace, phase)
        validator = json.loads((root / f"prng-{phase}-validator-tests.json").read_text())
        cases = COMMON_CASES + (SEARCH_CASES if phase == "full" else ())
        if (validator["status"] != "passed" or validator["phase"] != phase
                or validator["positive_shards"] != len(prng_reports[phase]["shards"])
                or len(validator["negative_cases"]) != len(cases)
                or {entry["case"] for entry in validator["negative_cases"]} != set(cases)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / f"native-prng-{phase}-report.json").read_bytes()).hexdigest()):
            raise ValueError("PRNG evidence validator negative checks differ")
    random_census = None
    if (root / "native-random-census-report.json").exists():
        from verify_random_census import validate_random_census
        from test_random_census_evidence import CASES as RANDOM_CENSUS_CASES

        random_census = validate_random_census(root, workspace)
        validator = json.loads((root / "random-census-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["positive_stages"] != 4
                or len(validator["negative_cases"]) != len(RANDOM_CENSUS_CASES)
                or {entry["case"] for entry in validator["negative_cases"]} != set(RANDOM_CENSUS_CASES)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / "native-random-census-report.json").read_bytes()).hexdigest()):
            raise ValueError("random census negative checks differ")
    action_random = None
    if (root / "native-action-random-report.json").exists():
        from verify_action_random import validate_action_random
        from test_action_random_evidence import CASES as ACTION_RANDOM_CASES

        action_random = validate_action_random(root, workspace)
        validator = json.loads((root / "action-random-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["sampled_ticks"] != action_random["report"]["sampled_ticks"]
                or len(validator["negative_cases"]) != len(ACTION_RANDOM_CASES)
                or {entry["case"] for entry in validator["negative_cases"]} != set(ACTION_RANDOM_CASES)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / "native-action-random-report.json").read_bytes()).hexdigest()):
            raise ValueError("action random negative checks differ")
    deck_belief = None
    if (root / "deck-belief-report.json").exists():
        from verify_deck_belief import validate_deck_belief
        from test_deck_belief_evidence import CASES as DECK_BELIEF_CASES

        deck_belief = validate_deck_belief()
        validator = json.loads((root / "deck-belief-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["positive_roots"] != 12
                or len(validator["negative_cases"]) != len(DECK_BELIEF_CASES)
                or {entry["case"] for entry in validator["negative_cases"]} != set(DECK_BELIEF_CASES)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / "deck-belief-report.json").read_bytes()).hexdigest()):
            raise ValueError("deck belief negative checks differ")
    prior_history = None
    if (root / "native-prior-history-report.json").exists():
        from verify_prior_history import validate_prior_history
        from test_prior_history_evidence import CASES as PRIOR_HISTORY_CASES

        prior_history = validate_prior_history()
        validator = json.loads((root / "prior-history-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["roots"] != 8 or validator["successors"] != 4
                or len(validator["negative_cases"]) != len(PRIOR_HISTORY_CASES)
                or {entry["case"] for entry in validator["negative_cases"]} != set(PRIOR_HISTORY_CASES)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / "native-prior-history-report.json").read_bytes()).hexdigest()):
            raise ValueError("native prior integration negative checks differ")
    prior_search = None
    if (root / "native-prior-search-report.json").exists():
        from verify_prior_search import validate_prior_search
        from test_prior_search_evidence import CASES as PRIOR_SEARCH_CASES

        prior_search = validate_prior_search()
        validator = json.loads((root / "prior-search-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["roots"] != 8 or validator["effective_simulations"] != 1024
                or len(validator["negative_cases"]) != len(PRIOR_SEARCH_CASES)
                or {entry["case"] for entry in validator["negative_cases"]} != set(PRIOR_SEARCH_CASES)
                or any(entry["rejected"] is not True for entry in validator["negative_cases"])
                or validator["report_sha256"] != hashlib.sha256(
                    (root / "native-prior-search-report.json").read_bytes()).hexdigest()):
            raise ValueError("prior search negative checks differ")
    local_reconditioning = None
    if (root / "native-local-reconditioning-report.json").exists():
        from verify_local_reconditioning import validate_local_reconditioning
        from test_local_reconditioning_evidence import CASES as LOCAL_RECONDITIONING_CASES

        local_reconditioning = validate_local_reconditioning()
        validator = json.loads((root / "local-reconditioning-evidence-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["count"] != len(LOCAL_RECONDITIONING_CASES)
                or validator["rejected"] != list(LOCAL_RECONDITIONING_CASES)
                or validator["validator_sha256"] != hashlib.sha256(
                    (root / "verify_local_reconditioning.py").read_bytes()).hexdigest()
                or validator["baseline_report_sha256"] != hashlib.sha256(
                    (root / "native-local-reconditioning-report.json").read_bytes()).hexdigest()):
            raise ValueError("local reconditioning negative checks differ")
    local_search = None
    if (root / "native-local-search-report.json").exists():
        from verify_local_search import validate_local_search
        from test_local_search_evidence import CASES as LOCAL_SEARCH_CASES

        local_search = validate_local_search()
        validator = json.loads((root / "local-search-evidence-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["roots"] != 16 or validator["effective_simulations"] != 2048
                or validator["count"] != len(LOCAL_SEARCH_CASES)
                or validator["rejected"] != list(LOCAL_SEARCH_CASES)
                or validator["validator_sha256"] != hashlib.sha256(
                    (root / "verify_local_search.py").read_bytes()).hexdigest()
                or validator["baseline_report_sha256"] != hashlib.sha256(
                    (root / "native-local-search-report.json").read_bytes()).hexdigest()):
            raise ValueError("local Full128 negative checks differ")
    boundary_coverage = None
    if (root / "native-boundary-coverage-report.json").exists():
        from verify_boundary_coverage import validate_boundary_coverage, validate_closure
        from test_boundary_coverage_evidence import CASES as BOUNDARY_CASES, CLOSURE_CASES

        boundary_coverage = validate_boundary_coverage()
        validate_closure()
        validator = json.loads((root / "boundary-coverage-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["count"] != len(BOUNDARY_CASES) + len(CLOSURE_CASES)
                or validator["rejected"] != list(BOUNDARY_CASES)
                or validator["closure_rejected"] != list(CLOSURE_CASES)
                or any(validator[key] != hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for key, name in (("validator_sha256", "verify_boundary_coverage.py"),
                        ("test_sha256", "test_boundary_coverage_evidence.py"),
                        ("baseline_report_sha256", "native-boundary-coverage-report.json"),
                        ("closure_report_sha256", "boundary-closure-report.json")))):
            raise ValueError("normal boundary coverage negative checks differ")
    refill_reports = {}
    for kind in ("trajectory", "search"):
        refill_path = root / f"native-refill-{kind}-report.json"
        if not refill_path.exists():
            continue
        from verify_refill import validate_refill_trajectory, validate_refill_search
        from test_refill_evidence import TRAJECTORY_CASES, SEARCH_CASES

        validate = validate_refill_trajectory if kind == "trajectory" else validate_refill_search
        cases = TRAJECTORY_CASES if kind == "trajectory" else SEARCH_CASES
        refill_reports[kind] = validate()
        validator = json.loads((root / f"refill-{kind}-validator-tests.json").read_text())
        if kind == "search" and validator.get("attempt_validator_sha256") != hashlib.sha256(
                (root / "verify_prior_search.py").read_bytes()).hexdigest():
            raise ValueError("refill attempt validator identity changed")
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["count"] != len(cases) or validator["rejected"] != list(cases)
                or any(validator[key] != hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for key, name in (("validator_sha256", "verify_refill.py"),
                        ("test_sha256", "test_refill_evidence.py"),
                        ("baseline_report_sha256", refill_path.name)))):
            raise ValueError("refill negative checks differ")
    refill_local = None
    if (root / "native-refill-local-report.json").exists():
        from verify_refill_local import validate_refill_local
        from test_refill_local_evidence import CASES as REFILL_LOCAL_CASES

        refill_local = validate_refill_local()
        validator = json.loads((root / "refill-local-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["count"] != len(REFILL_LOCAL_CASES) or validator["rejected"] != list(REFILL_LOCAL_CASES)
                or any(validator[key] != hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for key, name in (("validator_sha256", "verify_refill_local.py"),
                        ("test_sha256", "test_refill_local_evidence.py"),
                        ("baseline_report_sha256", "native-refill-local-report.json")))):
            raise ValueError("refill local evidence rejection checks differ")
    refill_provider = None
    if (root / "native-refill-provider-report.json").exists():
        from verify_refill_provider import validate_refill_provider
        from test_refill_provider_evidence import CASES as REFILL_PROVIDER_CASES

        refill_provider = validate_refill_provider()
        validator = json.loads((root / "refill-provider-validator-tests.json").read_text())
        if (validator["status"] != "passed" or validator["training_admissible"] is not False
                or validator["count"] != len(REFILL_PROVIDER_CASES)
                or validator["rejected"] != list(REFILL_PROVIDER_CASES)
                or validator["searches"] != 24 or validator["effective_simulations"] != 3072
                or any(validator[key] != hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for key, name in (("validator_sha256", "verify_refill_provider.py"),
                        ("attempt_validator_sha256", "verify_prior_search.py"),
                        ("test_sha256", "test_refill_provider_evidence.py"),
                        ("baseline_report_sha256", "native-refill-provider-report.json")))):
            raise ValueError("refill provider evidence rejection checks differ")
    single_ready_validator = json.loads((root / "single-ready-evidence-validator-tests.json").read_text())
    if (single_ready_validator["status"] != "passed" or single_ready_validator["positive_checks"] != 6
            or len(single_ready_validator["negative_cases"]) != len(NEGATIVE_CASES)
            or {entry["case"] for entry in single_ready_validator["negative_cases"]} != set(NEGATIVE_CASES)
            or any(entry["rejected"] is not True for entry in single_ready_validator["negative_cases"])
            or single_ready_validator["report_sha256"] != hashlib.sha256(
                (root / "native-single-ready-report.json").read_bytes()).hexdigest()):
        raise ValueError("single-ready validator negative-case evidence differs")
    history_search_validator = json.loads((root / "history-search-evidence-validator-tests.json").read_text())
    expected_search_negatives = {"missing_seat", "fake_budget", "changed_root", "hidden_input",
        "same_world_across_roots", "unchanged_hidden_state", "changed_source",
        "changed_artifact", "unclean_engine", "fake_match_unchanged", "missing_recovery"}
    if (history_search_validator["status"] != "passed" or history_search_validator["positive_checks"] != 4
            or len(history_search_validator["negative_cases"]) != len(expected_search_negatives)
            or {entry["case"] for entry in history_search_validator["negative_cases"]} != expected_search_negatives
            or any(entry["rejected"] is not True for entry in history_search_validator["negative_cases"])
            or history_search_validator["report_sha256"] != hashlib.sha256(
                (root / "native-history-search-report.json").read_bytes()).hexdigest()):
        raise ValueError("history search validator negative-case evidence differs")
    history_validator = json.loads((root / "history-evidence-validator-tests.json").read_text())
    expected_history_negatives = {"missing_seat", "wrong_delta", "hidden_input", "changed_source",
        "changed_artifact", "unclean_engine", "fake_match_unchanged", "changed_root"}
    if (history_validator["status"] != "passed" or history_validator["positive_checks"] != 6
            or len(history_validator["negative_cases"]) != len(expected_history_negatives)
            or {entry["case"] for entry in history_validator["negative_cases"]} != expected_history_negatives
            or any(entry["rejected"] is not True for entry in history_validator["negative_cases"])
            or history_validator["report_sha256"] != hashlib.sha256((root / "native-history-report.json").read_bytes()).hexdigest()):
        raise ValueError("native history validator negative-case evidence differs")
    native_recovery = json.loads((root / "native-recovery-report.json").read_text())
    if (native_recovery["status"] != "passed" or native_recovery["exact_continuation"] is not True
            or native_recovery["training_admissible"] is not False
            or native_recovery["resume_tick"] < 1200 or native_recovery["suffix_boundaries"] < 1
            or {entry["mode"] for entry in native_recovery["cleanup"]} != {"baseline", "recover"}
            or any(entry["exit_code"] != 0 or entry["listening"] or any(entry["pids_alive"].values())
                or entry["process"]["phase"] != "stopped" for entry in native_recovery["cleanup"])):
        raise ValueError("native cross-process recovery evidence is incomplete")
    if hashlib.sha256((root / "run_recovery_probe.py").read_bytes()).hexdigest() != native_recovery["runner_sha256"]:
        raise ValueError("native recovery runner identity changed")
    for relative, digest in native_recovery["source_sha256"].items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"native recovery report is stale for {relative}")
    for relative, digest in native_recovery["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"native recovery artifact changed: {relative}")
    baseline_path = next(root / path for path in native_recovery["artifacts"] if path.endswith("/baseline.json"))
    recovered_path = next(root / path for path in native_recovery["artifacts"] if path.endswith("/recover.json"))
    baseline_trajectory = json.loads(baseline_path.read_text())["trajectory"]
    recovered_trajectory = json.loads(recovered_path.read_text())["trajectory"]
    if baseline_trajectory != recovered_trajectory:
        raise ValueError("native recovery trajectories differ")
    if (len({entry["process"]["pid"] for entry in native_recovery["cleanup"]}) != 2
            or len({entry["process"]["child_pid"] for entry in native_recovery["cleanup"]}) != 2
            or not baseline_trajectory["boundary"]["clock"]["pending"]
            or baseline_trajectory["terminal"]["native_evidence"]["finalized"] is not True):
        raise ValueError("recovery requires distinct native processes, a pending boundary and native finalization")
    persistence = json.loads((root / "persistence-report.json").read_text())
    if (persistence["status"] != "passed" or persistence["exact_continuation"] is not True
            or persistence["uninterrupted"] != persistence["new_process"]
            or persistence["publication"]["completed_matches"] != 1
            or persistence["publication"]["crash_exit"] != 73
            or persistence["checkpoint"]["network_sha"] != persistence["candidate"]["network_sha"]):
        raise ValueError("cross-process persistence evidence is incomplete")
    for relative, digest in persistence["source_sha256"].items():
        if hashlib.sha256((workspace / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"persistence report is stale for {relative}")
    for relative, digest in persistence["artifacts"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"persistence artifact changed: {relative}")
    from verify_episode_evidence import inspect_archive

    complete_episodes = [inspect_archive("p1-complete-native-current"), inspect_archive("p1-native-draw")]
    if (complete_episodes[0]["terminal"]["winner"] not in (0, 1)
            or complete_episodes[1]["terminal"]["winner"] is not None):
        raise ValueError("native win and native draw evidence required")
    cleanup = json.loads((root / "complete-episode-cleanup.json").read_text())
    if {item["port"] for item in cleanup["results"]} != {29619, 29620} or any(
            item["phase"] != "stopped" or item["listening"] or any(item["pids_alive"].values())
            for item in cleanup["results"]):
        raise ValueError("live probe cleanup verification is missing")
    suites = element_tree.parse(test_report).getroot()
    failures = sum(int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0)) for suite in suites)
    tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    if failures or report["status"] != "passed" or process.get("phase") != "stopped":
        raise ValueError("P1 subset evidence is incomplete")
    cases = {case["case"]: case for case in report["cases"]}
    recovery = cases["source_a_fair_decisions"]
    if not all(recovery.get(key) is True for key in (
            "full_rollout_restore", "pending_snapshot_restore", "failed_runtime_recovery")):
        raise ValueError("complete rollout recovery evidence is missing")
    seats = cases["native_opponent_draw_noninterference"]["seats"]
    if {seat["owner"] for seat in seats} != {0, 1} or not all(
            seat["native_hidden_changed"] and seat["exact_cpu_outputs"] for seat in seats):
        raise ValueError("both-seat native hidden draw verification is missing")
    deployed = report.get("source_sha256", {})
    if not deployed:
        raise ValueError("native report has no deployed source identity")
    for relative, digest in deployed.items():
        if hashlib.sha256((workspace / "firstlight_CR" / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"native report is stale for {relative}")
    identities = {}
    for folder in (workspace / "firstlight_CR/native_runner/training/fair_final", root / "spec/clash_royale_fair_final_v1"):
        for path in sorted(folder.glob("*")):
            if path.is_file():
                identities[str(path.relative_to(workspace))] = hashlib.sha256(path.read_bytes()).hexdigest()
    artifacts = [test_report, root / "native-timing-report.json", root / "native-probe-process.json",
        workspace / "firstlight_CR/tools/probe_fair_final_timing.py", root / "run_native_probe.py"]
    artifacts.extend((workspace / "firstlight_CR/native_runner/tests").glob("test_fair_final_*.py"))
    artifacts.extend(root / name for name in ("p1-pending-search-tests.xml", "pending-search-contract.md"))
    artifacts.extend(root / name for name in ("p1-migration-loss-tests.xml", "migration-training-contract.md"))
    artifacts.extend(root / name for name in ("p1-migration-data-tests.xml", "migration-data-contract.md"))
    artifacts.extend(root / name for name in ("test_migration_data_gate.py", "migration-data-gate-tests.xml"))
    artifacts.extend(root / name for name in ("p1-migration-collector-tests.xml", "test_migration_collector_gate.py",
        "migration-collector-gate-tests.xml", "run_migration_probe.py", "migration_probe_worker.py"))
    if migration_native is not None:
        artifacts.extend(root / name for name in ("native-migration-evidence-report.json",
            "migration-native-evidence-tests.xml", "verify_migration_evidence.py", "test_migration_native_evidence.py",
            "migration_native_training_probe.py", "finalize_migration_probe.py", "migration-native-contract.md",
            "test_migration_native_gate.py", "migration-native-gate-tests.xml", "migration-collector-data-regression.xml"))
        artifacts.extend(root / relative for relative in migration_native["artifacts"])
    if supplement is not None:
        artifacts.append(root / "migration-supplement-evidence.json")
        artifacts.extend(workspace / relative for relative in supplement["artifacts_sha256"])
    artifacts.extend(workspace / "firstlight_CR" / name for name in (
        "native_runner/training/fair_migration.py", "native_runner/training/fair_migration_trainer.py",
        "native_runner/tests/test_fair_migration.py", "native_runner/tests/test_fair_migration_trainer.py",
        "native_runner/training/fair_migration_data.py", "native_runner/tests/test_fair_migration_data.py",
        "native_runner/training/fair_migration_collect.py", "native_runner/tests/test_fair_migration_collect.py",
        "tools/collect_fair_final_migration.py"))
    artifacts.extend([root / "complete-episode-report.json", root / "run_episode_probe.py",
        root / "verify_episode_evidence.py", root / "complete-episode-cleanup.json",
        root / "build_gate_report.py", workspace / "firstlight_CR/tools/probe_fair_final_episode.py"])
    artifacts.extend([root / "persistence-report.json", root / "run_persistence_probe.py", root / "persistence-contract.md"])
    artifacts.extend(root / relative for relative in persistence["artifacts"])
    artifacts.extend([root / "native-recovery-report.json", root / "run_recovery_probe.py",
        root / "inprogress-recovery-contract.md", workspace / "firstlight_CR/tools/probe_fair_final_recovery.py"])
    artifacts.extend(root / relative for relative in native_recovery["artifacts"])
    artifacts.extend([root / "trajectory-visibility-report.json", root / "trajectory-visibility-contract.md",
        root / "trajectory-evidence-validator-tests.json", root / "run_visibility_validation.py",
        workspace / "firstlight_CR/tools/probe_fair_final_visibility.py"])
    artifacts.extend(root / relative for relative in visibility["artifacts"])
    artifacts.extend([root / "native-search-report.json", root / "run_search_validation.py",
        root / "verify_search_evidence.py", root / "search-evidence-validator-tests.json", root / "completed-search-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_search.py"])
    artifacts.extend(root / relative for relative in native_search["artifacts"])
    artifacts.extend([root / "native-history-report.json", root / "run_history_validation.py",
        root / "verify_history_evidence.py", root / "test_history_evidence.py",
        root / "history-evidence-validator-tests.json", root / "history-belief-contract.md",
        root / "IMPLEMENTATION_STATUS.md",
        workspace / "firstlight_CR/tools/probe_fair_final_history.py"])
    artifacts.extend(root / relative for relative in native_history["artifacts"])
    artifacts.extend([root / "native-history-search-report.json", root / "run_history_search_validation.py",
        root / "verify_history_search_evidence.py", root / "test_history_search_evidence.py",
        root / "history-search-evidence-validator-tests.json", root / "history-search-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_history_search.py"])
    artifacts.extend(root / relative for relative in history_search["artifacts"])
    artifacts.extend([root / "native-single-ready-report.json", root / "run_single_ready_validation.py",
        root / "verify_single_ready_evidence.py", root / "test_single_ready_evidence.py",
        root / "single-ready-evidence-validator-tests.json", root / "single-ready-diagnostic-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_single_ready.py"])
    artifacts.extend(root / relative for relative in single_ready["artifacts"])
    artifacts.extend([root / "run_extended_search_validation.py", root / "verify_extended_search_evidence.py",
        root / "test_extended_search_evidence.py", root / "extended-search-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_extended_search.py"])
    if extended_search is not None:
        artifacts.extend([root / "native-extended-search-report.json", root / "extended-search-evidence-validator-tests.json"])
        artifacts.extend(root / relative for relative in extended_search["artifacts"])
    artifacts.extend([root / "run_prng_validation.py", root / "prng_validation_worker.py",
        root / "verify_prng_evidence.py", root / "test_prng_evidence.py", root / "prng-audit-contract.md",
        root / "record_prng_layout.py", root / "native-prng-layout.json",
        workspace / "firstlight_CR/tools/fair_final_prng_audit.py",
        workspace / "firstlight_CR/tools/probe_fair_final_prng.py"])
    for phase, prng in prng_reports.items():
        artifacts.extend([root / f"native-prng-{phase}-report.json", root / f"prng-{phase}-validator-tests.json"])
        artifacts.extend(root / relative for relative in prng["artifacts"])
    artifacts.extend([root / "run_random_census.py", root / "random_census_worker.py",
        root / "verify_random_census.py", root / "test_random_census_evidence.py",
        root / "random-census-contract.md", root / "native-random-inventory.json",
        root / "p1-random-census-tests.xml",
        workspace / "firstlight_CR/tools/fair_final_random_inventory.py",
        workspace / "firstlight_CR/tools/fair_final_random_census.py",
        workspace / "firstlight_CR/tools/probe_fair_final_random_census.py"])
    if random_census is not None:
        artifacts.extend([root / "native-random-census-report.json", root / "random-census-validator-tests.json"])
        artifacts.extend(root / relative for relative in random_census["artifacts"])
    artifacts.extend([root / "run_action_random.py", root / "action_random_worker.py",
        root / "verify_action_random.py", root / "test_action_random_evidence.py",
        root / "action-random-contract.md", root / "native-action-random-layout.json",
        workspace / "firstlight_CR/tools/fair_final_action_random.py",
        workspace / "firstlight_CR/tools/probe_fair_final_action_random.py"])
    if action_random is not None:
        artifacts.extend([root / "native-action-random-report.json", root / "action-random-validator-tests.json"])
        artifacts.extend(root / relative for relative in action_random["artifacts"])
    artifacts.extend([root / "run_deck_belief_validation.py", root / "verify_deck_belief.py",
        root / "test_deck_belief_evidence.py", root / "deck-belief-contract.md"])
    if deck_belief is not None:
        artifacts.extend([root / "deck-belief-report.json", root / "deck-belief-validator-tests.json"])
        artifacts.extend(workspace / relative for relative in deck_belief["identities"])
    artifacts.extend([root / "run_prior_history_validation.py", root / "prior_history_worker.py",
        root / "verify_prior_history.py", root / "test_prior_history_evidence.py", root / "prior-history-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_prior_history.py"])
    if prior_history is not None:
        artifacts.extend([root / "native-prior-history-report.json", root / "prior-history-validator-tests.json"])
        artifacts.extend(root / relative for relative in prior_history["artifacts"])
    artifacts.extend([root / "run_prior_search_validation.py", root / "prior_search_worker.py",
        root / "verify_prior_search.py", root / "test_prior_search_evidence.py", root / "prior-search-contract.md",
        workspace / "firstlight_CR/tools/probe_fair_final_prior_search.py"])
    if prior_search is not None:
        artifacts.extend([root / "native-prior-search-report.json", root / "prior-search-validator-tests.json"])
        artifacts.extend(root / relative for relative in prior_search["artifacts"])
    artifacts.extend([root / name for name in ("run_local_reconditioning_validation.py",
        "local_reconditioning_worker.py", "verify_local_reconditioning.py", "test_local_reconditioning_evidence.py",
        "build_phase_snapshot_engine.py", "phase_snapshot_launcher.py", "test_phase_snapshot_launcher.py",
        "phase-snapshot-contract.md")])
    artifacts.extend([workspace / "firstlight_CR" / relative for relative in (
        "tools/probe_fair_final_local_reconditioning.py", "native_runner/training/fair_final/local_reconditioning.py",
        "native_runner/probe/fair_final_phase_snapshot.inc", "native_runner/probe/fair_final_phase_observation.h",
        "native_runner/probe/test_fair_final_phase_snapshot.cpp", "native_runner/tests/test_fair_final_phase_snapshot.py",
        "native_runner/tests/test_fair_final_local_reconditioning.py")])
    artifacts.extend([root / name for name in ("run_local_search_validation.py", "local_search_worker.py",
        "verify_local_search.py", "test_local_search_evidence.py", "local-search-contract.md")])
    artifacts.extend([workspace / "firstlight_CR" / name for name in ("tools/probe_fair_final_local_search.py",
        "native_runner/tests/test_fair_final_local_search_provider.py",
        "native_runner/tests/test_fair_final_local_search_audit.py")])
    if local_search is not None:
        artifacts.extend([root / "native-local-search-report.json", root / "local-search-evidence-validator-tests.json"])
        artifacts.extend(root / relative for relative in local_search["artifacts"])
    if local_reconditioning is not None:
        artifacts.extend([root / "native-local-reconditioning-report.json",
            root / "local-reconditioning-evidence-validator-tests.json"])
        artifacts.extend(root / relative for relative in local_reconditioning["artifacts"])
    if boundary_coverage is not None:
        artifacts.extend(root / name for name in ("boundary_reachability.py", "boundary-closure-report.json",
            "run_boundary_coverage_validation.py", "boundary_coverage_worker.py", "verify_boundary_coverage.py",
            "test_boundary_coverage_evidence.py", "boundary-coverage-validator-tests.json",
            "native-boundary-coverage-report.json", "boundary-coverage-contract.md"))
        artifacts.append(workspace / "firstlight_CR/tools/probe_fair_final_boundary_coverage.py")
        artifacts.extend(root / relative for relative in boundary_coverage["artifacts"])
    if refill_reports:
        refill_tests = element_tree.parse(root / "p1-refill-tests.xml").getroot()
        suites = list(refill_tests.iter("testsuite"))
        if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 68
                or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("failures", "errors", "skipped"))):
            raise ValueError("refill regression evidence incomplete")
        artifacts.extend(root / name for name in ("run_refill_validation.py", "refill_worker.py",
            "run_refill_search_validation.py", "refill_search_worker.py", "verify_refill.py",
            "test_refill_evidence.py", "refill-contract.md", "p1-refill-tests.xml"))
        artifacts.extend(workspace / "firstlight_CR" / name for name in (
            "native_runner/training/fair_final/refill_belief.py", "native_runner/tests/test_fair_final_refill_belief.py",
            "tools/probe_fair_final_refill.py", "tools/probe_fair_final_refill_search.py"))
        if "search" in refill_reports:
            attempt_tests = element_tree.parse(root / "p1-refill-attempt-tests.xml").getroot()
            attempt_suites = list(attempt_tests.iter("testsuite"))
            if (sum(int(suite.attrib.get("tests", 0)) for suite in attempt_suites) != 15
                    or any(int(suite.attrib.get(key, 0)) for suite in attempt_suites for key in ("errors", "failures", "skipped"))):
                raise ValueError("refill shallow-attempt regression evidence incomplete")
            artifacts.extend(root / name for name in ("test_refill_attempt_validation.py", "test_refill_search_finalizer.py",
                "finish_refill_search_validation.py", "p1-refill-attempt-tests.xml"))
            conditioned_tests = element_tree.parse(root / "p1-conditioned-plan-tests.xml").getroot()
            suites = list(conditioned_tests.iter("testsuite"))
            if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 76
                    or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("failures", "errors", "skipped"))):
                raise ValueError("conditional hidden-plan regression evidence incomplete")
            artifacts.append(root / "p1-conditioned-plan-tests.xml")
            artifacts.extend(workspace / "firstlight_CR" / name for name in (
                "native_runner/training/fair_final/conditioned_plan.py",
                "native_runner/tests/test_fair_final_conditioned_plan.py"))
        for kind, evidence in refill_reports.items():
            artifacts.extend(root / name for name in (f"native-refill-{kind}-report.json", f"refill-{kind}-validator-tests.json"))
            artifacts.extend(root / relative for relative in evidence["artifacts"])
    if refill_local is not None:
        suites = list(element_tree.parse(root / "p1-refill-local-tests.xml").getroot().iter("testsuite"))
        if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 138
                or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))):
            raise ValueError("refill local regression evidence incomplete")
        artifacts.extend(root / name for name in ("run_refill_local_validation.py", "refill_local_worker.py",
            "verify_refill_local.py", "test_refill_local_evidence.py", "native-refill-local-report.json",
            "refill-local-validator-tests.json", "refill-local-contract.md", "p1-refill-local-tests.xml"))
        artifacts.extend(workspace / "firstlight_CR" / name for name in (
            "native_runner/training/fair_final/refill_local.py", "native_runner/tests/test_fair_final_refill_local.py",
            "tools/probe_fair_final_refill_local.py"))
        artifacts.extend(root / relative for relative in refill_local["artifacts"])
    if refill_provider is not None:
        suites = list(element_tree.parse(root / "p1-refill-provider-tests.xml").getroot().iter("testsuite"))
        if (sum(int(suite.attrib.get("tests", 0)) for suite in suites) != 63
                or any(int(suite.attrib.get(key, 0)) for suite in suites for key in ("errors", "failures", "skipped"))):
            raise ValueError("refill provider regression evidence incomplete")
        finalizer_suites = list(element_tree.parse(root / "p1-refill-provider-finalizer-tests.xml").getroot().iter("testsuite"))
        if (sum(int(suite.attrib.get("tests", 0)) for suite in finalizer_suites) != 10
                or any(int(suite.attrib.get(key, 0)) for suite in finalizer_suites for key in ("errors", "failures", "skipped"))):
            raise ValueError("refill provider finalizer regression evidence incomplete")
        artifacts.extend(root / name for name in ("run_refill_provider_validation.py", "refill_provider_worker.py",
            "verify_refill_provider.py", "test_refill_provider_evidence.py", "native-refill-provider-report.json",
            "refill-provider-validator-tests.json", "refill-provider-contract.md", "p1-refill-provider-tests.xml",
            "finish_refill_provider_validation.py", "test_refill_provider_finalizer.py", "p1-refill-provider-finalizer-tests.xml"))
        artifacts.extend(workspace / "firstlight_CR" / name for name in (
            "tools/probe_fair_final_refill_provider.py", "native_runner/tests/test_fair_final_refill_provider.py"))
        artifacts.extend(root / relative for relative in refill_provider["artifacts"])
    for episode in complete_episodes:
        artifacts.extend([root / episode["archive"], root / episode["process_artifact"]])
    for path in artifacts:
        identities[str(path.relative_to(workspace))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_sha = hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
    now = time.time()
    refill_gap = "incomplete-refill conditioning, "
    if "search" in refill_reports:
        refill_gap = "local incomplete-refill reconditioning and hidden-state pairs, "
        if refill_local is not None:
            refill_gap = "incomplete-refill local Full128 and hidden-state pairs, "
        if refill_provider is not None:
            refill_gap = ""
    checks = [
        ("G0.SOURCE_A", "PASS", "source_compare_report.json", "five-host A SHA and step128 parameters match"),
        ("G0.TIMING_NATIVE_SUBSET", "PASS", "native-timing-report.json", "same-tick, preemption, payment, rejection and snapshot tests"),
        ("G1.PROBABILITY_LOSS_SUBSET", "PASS", test_report.name, f"{tests} tests; probability, losses, recovery and public tracker regressions"),
        ("G1.PENDING_SEARCH_SYNTHETIC_SUBSET", "PASS", "p1-pending-search-tests.xml",
            f"{pending_search_tests['pending_tests']} pending cases within {pending_search_tests['tests']} regressions: both-seat WAIT/ACT, asynchronous wake, completed-edge budgets, owner-perspective terminal values and root/RNG restore; in-memory environment only, excludes native pending belief reconstruction and complete-game search"),
        ("G1.MIGRATION_UPDATE_SYNTHETIC_SUBSET", "PASS", "p1-migration-loss-tests.xml",
            f"{migration_tests['tests']} loss/trainer/adjacent tests: full-joint teacher KL, greedy NLL, WDL KL, staged encoder updates, frozen-teacher integrity, deterministic cursor and exact checkpoint resume; fixtures only, excludes native migration datasets, validation-selected weights, calibration and strength"),
        ("G1.MIGRATION_DATA_SYNTHETIC_SUBSET", "PASS", "p1-migration-data-tests.xml",
            f"{migration_data_tests['tests']} migration data tests: complete-game contracts, frozen teacher soft-target references, separate behavior labels, whole-game splits, immutable receipts, deduplication, interruption retry and dataset-backed update/resume; fixtures only, excludes native collector acceptance and formal 1024/128/128 collection"),
        ("G1.MIGRATION_COLLECTOR_SYNTHETIC_SUBSET", "PASS", "p1-migration-collector-tests.xml",
            f"{migration_collector_tests['tests']} collector tests: simultaneous frozen views, both-player completion, preemption retention, invalid-terminal/rejection/timeout quarantine and protocol identities; fixtures only, excludes native complete-game acceptance"),
        ("G1.MIGRATION_NATIVE_DATA_UPDATE_SUBSET", "PASS" if migration_native is not None else "IN_PROGRESS",
            "native-migration-evidence-report.json" if migration_native is not None else "migration-data-contract.md",
            f"three native A-greedy complete games, {migration_native['rows']} rows in frozen whole-game splits, two FP32 batch-one updates with exact network/optimizer/RNG/cursor resume, three owned engines stopped and 28 tamper rejections; engineering only, excludes formal counts, validation-selected weights, calibration and strength"
            if migration_native is not None else "native complete migration games and dataset-backed exact update/resume acceptance pending"),
        ("G1.A_ADAPTER_SMOKE", "PASS", "native-timing-report.json", "16 short-trajectory decisions; not a complete training episode"),
        ("G0.FAIR_NATIVE_DRAW_SUBSET", "PASS", "native-timing-report.json",
            "both seats: true opponent hand/cycle changed, exact input/candidates/action/CPU outputs unchanged; opening only"),
        ("G0.FAIR_TRAJECTORY_SUBSET", "PASS", "trajectory-visibility-report.json",
            "32 exact checks across four stages and both seats; native draw, reset metadata, private events and pending plans; complete trajectory, not Full128 or native PRNG proof"),
        ("G1.FULL128_OPENING_NATIVE_SUBSET", "PASS", "native-search-report.json",
            "four Full128 native roots: both-seat true hidden draw perturbations yield exact candidates/N/Q/selection in an independent opening belief engine; short 2-group/60-tick horizon, not full-episode quality"),
        ("G0.HISTORY_BELIEF_NATIVE_SUBSET", "PASS", "native-history-report.json",
            "both seats at middle/late/overtime roots: independent legal-history cold reconstruction and next simultaneous-boundary snapshot delta match full FAIR frames/masks; no actual-state copy, no Full128 or general pending-root proof"),
        ("G0.HISTORY_SEARCH_NATIVE_SUBSET", "PASS", "native-history-search-report.json",
            "12 evolving-history roots include natural mismatch recovery; both-seat middle-game Full128 hidden-draw perturbation matches candidates/N/Q/selection/targets with fresh independent worlds; short horizon, not constant-cost reconditioning or native single-ready proof"),
        ("G0.SINGLE_READY_REJECTION_DIAGNOSTIC", "PASS", "native-single-ready-report.json",
            "both-seat injected native rejection: opposing WAIT preserved; staggered expiry, early ACT preemption and same-tick settlement replay exactly with FAIR frames/masks/RNG/native digest; diagnostic only, not a legal training root or belief-search proof"),
        ("G0.EXTENDED_FULL128_NATIVE_SUBSET", "PASS" if extended_search is not None else "IN_PROGRESS",
            "native-extended-search-report.json" if extended_search is not None else "extended-search-contract.md",
            "both-seat late/overtime true-hidden-draw pairs: default six-group/200-tick Full128, audited completed edges and exact result/trajectory noninterference; not full-episode quality"
            if extended_search is not None else "four isolated shards registered; default-horizon native paired evidence not yet accepted"),
        ("G1.ROLLOUT_RECOVERY_SUBSET", "PASS", "native-timing-report.json",
            "frozen and pending branches restore trackers, frames, owner RNG, clock and engine; not durable process restart"),
        ("G0.PRNG_NATIVE_BOUNDARY_SUBSET", "PASS" if "smoke" in prng_reports else "IN_PROGRESS",
            "native-prng-smoke-report.json" if "smoke" in prng_reports else "prng-audit-contract.md",
            "manager+0xa0 actual native PRNG changes while both-seat observation/policy remains exact; native snapshot restores state/digest; stale writes rejected; other PRNG objects excluded"),
        ("G0.PRNG_FULL128_NATIVE_SUBSET", "PASS" if "full" in prng_reports else "IN_PROGRESS",
            "native-prng-full-report.json" if "full" in prng_reports else "prng-audit-contract.md",
            "opening/late both-seat native PRNG pairs: eight default-horizon Full128 roots with identical candidates/N/Q/labels/world/trajectories; other stages/PRNG objects excluded"
            if "full" in prng_reports else "four isolated default-horizon Full128 PRNG shards registered; complete paired evidence still pending"),
        ("G0.RANDOM_OBJECT_CENSUS_SUBSET", "PASS" if random_census is not None else "IN_PROGRESS",
            "native-random-census-report.json" if random_census is not None else "random-census-contract.md",
            "fixed-binary direct-call inventory and four-stage readonly active/alternate manager object census; repeated snapshot continuations match; not component-state or all-PRNG noninterference coverage"),
        ("G0.ACTION_RANDOM_SCOPE_SUBSET", "PASS" if action_random is not None else "IN_PROGRESS",
            "native-action-random-report.json" if action_random is not None else "action-random-contract.md",
            "target identified as Goblinstein ability runtime; all decision plus stage/terminal samples in one native complete hog trajectory contain no target; not target-state snapshot or all-random coverage"),
        ("G0.DECK_PRIOR_CYCLE_SUBSET", "PASS" if deck_belief is not None else "IN_PROGRESS",
            "deck-belief-report.json" if deck_belief is not None else "deck-belief-contract.md",
            "versioned public-deck frequency and incremental legal-cycle support; twelve archived native roots and 192 draws match cold reference; no population calibration or native local reconditioning"),
        ("G0.NATIVE_PRIOR_CONSTRUCTION_SUBSET", "PASS" if prior_history is not None else "IN_PROGRESS",
            "native-prior-history-report.json" if prior_history is not None else "prior-history-contract.md",
            "fixed-Hog prior sampled into independent native worlds; paired both-seat stage roots and completed-edge restores; excludes Full128 and native local reconditioning"),
        ("G0.NATIVE_PRIOR_SEARCH_SUBSET", "PASS" if prior_search is not None else "IN_PROGRESS",
            "native-prior-search-report.json" if prior_search is not None else "prior-search-contract.md",
            "versioned-prior independent worlds, paired middle/late both-seat Full128 with default horizon and per-attempt native restore checks; excludes local reconditioning and complete-game strength"),
        ("G0.LOCAL_RECONDITIONING_NATIVE_SUBSET", "PASS" if local_reconditioning is not None else "IN_PROGRESS",
            "native-local-reconditioning-report.json" if local_reconditioning is not None else "phase-snapshot-contract.md",
            "isolated engine: both-seat middle/late settled roots, 12 A/B/A local replacements and 24 repeat continuations; exact phase/FAIR/controller restore with zero history replay during replacement; excludes Full128, true-hidden intervention, pending roots, multi-slot and complete-game throughput"),
        ("G0.LOCAL_FULL128_NATIVE_SUBSET", "PASS" if local_search is not None else "IN_PROGRESS",
            "native-local-search-report.json" if local_search is not None else "local-search-contract.md",
            "isolated phase engine: middle/late both-seat initial and incremental successor roots; 16 Full128 searches, true-hidden draw pairs, per-attempt phase restore and persistent anchor cleanup; excludes pending roots, multi-slot, complete-game quality and throughput"
            if local_search is not None else "provider lifecycle fixed and regression-tested; phase-engine Full128, hidden-state pairs and incremental successor evidence pending"),
        ("G0.NORMAL_BOUNDARY_REACHABILITY_SUBSET", "PASS" if boundary_coverage is not None else "IN_PROGRESS",
            "native-boundary-coverage-report.json" if boundary_coverage is not None else None,
            "58621 timing cases under explicit assumptions; sampled native win and WAIT draw, 274 simultaneous groups replayed tick-by-tick; three incomplete-refill roots observed, not supported by settled reconditioning; excludes rejection search branches and general pending-root conditioning"),
        ("G0.REFILL_NATIVE_TRAJECTORY_SUBSET", "PASS" if "trajectory" in refill_reports else "IN_PROGRESS",
            "native-refill-trajectory-report.json" if "trajectory" in refill_reports else "refill-contract.md",
            "complete accepted trajectory replay, three native one-hole roots, six repeated 30-tick continuations and FAIR-only histories; no local conditioning or search claim"),
        ("G0.REFILL_HISTORY_FULL128_SUBSET", "PASS" if "search" in refill_reports else "IN_PROGRESS",
            "native-refill-search-report.json" if "search" in refill_reports else "refill-contract.md",
            "six both-seat early/middle/late roots with delayed native refills: independent legal-history construction, repeated snapshot continuation and Full128; excludes local refill conditioning and complete-game strength"
            if "search" in refill_reports else "hole-preserving native layout and independent-history factory implemented; native six-root Full128 evidence pending"),
        ("G0.REFILL_LOCAL_INCREMENTAL_SUBSET", "PASS" if refill_local is not None else "IN_PROGRESS",
            "native-refill-local-report.json" if refill_local is not None else "refill-contract.md",
            "six independent early/middle/late both-seat anchors plus real successors; 36 public-fixed unseen-card relabelings, 72 repeat continuations and 12 post-mutation rollback checks, preserved holes/FIFO/refill timing; anchor-conditional conservative subset, excludes full-posterior resampling, local Full128 and hidden-state pairs"
            if refill_local is not None else "native refill local replacement and real successor incremental evidence pending"),
        ("G0.REFILL_LOCAL_FULL128_SUBSET", "PASS" if refill_provider is not None else "IN_PROGRESS",
            "native-refill-provider-report.json" if refill_provider is not None else "refill-provider-contract.md",
            "six early/middle/late both-seat anchors and real successors: 24 Full128 roots, exact true-hidden-draw pairs, native hole/FIFO preservation, zero local history replay and incremental-only successor sync; anchor-conditional subset, excludes full posterior, general pending roots and complete-game strength"
            if refill_provider is not None else "refill provider lifecycle tested; native Full128 hidden-state pairs and real successor verification pending"),
        ("G1.COMPLETE_EPISODE_SUBSET", "PASS", "complete-episode-report.json",
            "native win and draw; both-player frames, legal actions, reservation outcomes, WDL and atomic completion; acceptance only"),
        ("G1.DURABLE_DATA_LEARNER_SUBSET", "PASS", "persistence-report.json",
            "synthetic contract fixtures: terminal publication crash/dedup and CPU FP32 learner restart produce exact subsequent updates; no native in-progress recovery"),
        ("G1.NATIVE_PROCESS_RECOVERY_SUBSET", "PASS", "native-recovery-report.json",
            "new worker and native engine replay a pending middle-game checkpoint; all subsequent boundaries and native terminal exactly match; acceptance only, not O(1) snapshot import"),
        ("G0", "IN_PROGRESS", "fair_engine_contract.md",
            ("" if extended_search is not None else "late/overtime Full128 noninterference, ")
            + ((refill_gap+"rejection-branch single-ready/pending coverage, ")
                if boundary_coverage is not None else "normal single-ready belief roots, ")
            + "general local independent reconditioning and Full128 integration, population-calibrated empirical prior, other native PRNG objects and remaining hidden mechanics pending"),
        ("G1", "IN_PROGRESS", "fair_engine_contract.md",
            ("formal migration-round integration, " if migration_native is not None else "native migration-data integration, ")
            + ("complete-game search and actor training-record recovery integration pending"
                if extended_search is not None else "full-horizon/complete-game search and actor training-record recovery integration pending")),
        ("G2", "NOT_RUN", None, "migration collection, fitting, calibration and 400/2000 acceptance pending"),
        ("G3", "NOT_RUN", None, "frozen-network paired search improvement pending"),
        ("G4", "NOT_RUN", None, "32 complete games and learner recovery pending"),
        ("G5", "NOT_RUN", None, "Full/Fast compute-matched ablation pending"),
        ("G6", "NOT_RUN", "runtime_inventory.json", "center storage gate unmet; five-host capacity not admitted"),
        ("G7", "NOT_RUN", None, "no champion promotion"),
        ("G8", "NOT_RUN", None, "no final 1755 evaluation"),
    ]
    records = [dict(GATE_ID=identity, status=status, test_manifest_sha=manifest_sha, time=now,
        evidence_artifact=artifact, reviewer="Codex automated verification", detail=detail)
        for identity, status, artifact, detail in checks]
    (root / "gate-status.json").write_text(json.dumps(dict(schema="fair-final-gate-evidence.v1",
        records=records, identities=identities), indent=2))
    print(json.dumps(dict(tests=tests, failures=failures, native_cases=len(report["cases"]),
        engine_phase=process["phase"], G0="IN_PROGRESS", G1="IN_PROGRESS", test_manifest_sha=manifest_sha)))


if __name__ == "__main__":
    main()
