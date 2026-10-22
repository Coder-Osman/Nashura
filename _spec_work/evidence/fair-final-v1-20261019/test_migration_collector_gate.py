from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as element_tree

import pytest

from build_gate_report import validate_migration_collector_tests


ROOT = Path(__file__).resolve().parent


def test_collector_gate_accepts_required_branches():
    assert validate_migration_collector_tests(ROOT / "p1-migration-collector-tests.xml") == dict(
        tests=13, native=False, formal_training=False)


@pytest.mark.parametrize("mutation", ["count", "missing", "duplicate", "name", "class", "failure_counter",
    "error_counter", "skip_counter", "failure", "error", "skipped"])
def test_collector_gate_rejects_incomplete_or_forged_report(tmp_path, mutation):
    report = element_tree.parse(ROOT / "p1-migration-collector-tests.xml")
    suite = next(report.getroot().iter("testsuite"))
    cases = list(suite.iter("testcase"))
    if mutation == "count":
        suite.set("tests", "12")
    elif mutation == "missing":
        suite.remove(cases[-1])
    elif mutation == "duplicate":
        suite.remove(cases[-1])
        suite.append(deepcopy(cases[0]))
    elif mutation == "name":
        cases[0].set("name", "other_branch")
    elif mutation == "class":
        cases[0].set("classname", "other.tests")
    elif mutation.endswith("_counter"):
        suite.set({"failure_counter": "failures", "error_counter": "errors", "skip_counter": "skipped"}[mutation], "1")
    else:
        element_tree.SubElement(cases[0], mutation)
    path = tmp_path / "report.xml"
    report.write(path)
    with pytest.raises(ValueError, match="migration collector"):
        validate_migration_collector_tests(path)
