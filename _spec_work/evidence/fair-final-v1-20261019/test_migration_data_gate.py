from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as element_tree

import pytest

from build_gate_report import validate_migration_data_tests


ROOT = Path(__file__).resolve().parent


def test_migration_data_gate_accepts_complete_evidence():
    assert validate_migration_data_tests(ROOT / "p1-migration-data-tests.xml") == dict(
        tests=47, native=False, formal_training=False)


@pytest.mark.parametrize("mutation", ["count", "missing", "duplicate", "renamed_case", "foreign_class",
    "failure_counter", "error_counter", "skip_counter", "hidden_failure", "hidden_error", "hidden_skip"])
def test_migration_data_gate_rejects_incomplete_or_forged_evidence(tmp_path, mutation):
    report = element_tree.parse(ROOT / "p1-migration-data-tests.xml")
    suite = next(report.getroot().iter("testsuite"))
    cases = list(suite.iter("testcase"))
    if mutation == "count":
        suite.set("tests", "46")
    elif mutation == "missing":
        suite.remove(cases[-1])
    elif mutation == "duplicate":
        suite.remove(cases[-1])
        suite.append(deepcopy(cases[0]))
    elif mutation == "renamed_case":
        cases[0].set("name", "missing_required_branch")
    elif mutation == "foreign_class":
        cases[0].set("classname", "unrelated.tests")
    elif mutation.endswith("_counter"):
        suite.set({"failure_counter": "failures", "error_counter": "errors", "skip_counter": "skipped"}[mutation], "1")
    else:
        element_tree.SubElement(cases[0],
            {"hidden_failure": "failure", "hidden_error": "error", "hidden_skip": "skipped"}[mutation])
    path = tmp_path / "report.xml"
    report.write(path)
    with pytest.raises(ValueError, match="migration data"):
        validate_migration_data_tests(path)
