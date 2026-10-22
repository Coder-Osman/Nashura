from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as element_tree

import pytest

from build_gate_report import validate_pending_search_tests


ROOT = Path(__file__).resolve().parent


def test_pending_search_accepts_complete_regression_evidence():
    assert validate_pending_search_tests(ROOT / "p1-pending-search-tests.xml") == dict(
        tests=106, pending_tests=32, native=False)


@pytest.mark.parametrize("mutation", ["count", "missing", "duplicate", "renamed_branch", "foreign_class",
    "failure_counter", "error_counter", "skip_counter", "hidden_failure", "hidden_error", "hidden_skip"])
def test_pending_search_rejects_incomplete_or_forged_evidence(tmp_path, mutation):
    report = element_tree.parse(ROOT / "p1-pending-search-tests.xml")
    suite = next(report.getroot().iter("testsuite"))
    cases = list(suite.iter("testcase"))
    pending = next(case for case in cases
        if case.attrib["classname"].endswith(".test_fair_final_pending_search"))
    if mutation == "count":
        suite.set("tests", "105")
    elif mutation == "missing":
        suite.remove(pending)
    elif mutation == "duplicate":
        suite.remove(cases[-1])
        suite.append(deepcopy(pending))
    elif mutation == "renamed_branch":
        pending.set("name", "uncovered_branch")
    elif mutation == "foreign_class":
        pending.set("classname", "unrelated.tests")
    elif mutation.endswith("_counter"):
        suite.set({"failure_counter": "failures", "error_counter": "errors", "skip_counter": "skipped"}[mutation], "1")
    else:
        element_tree.SubElement(pending,
            {"hidden_failure": "failure", "hidden_error": "error", "hidden_skip": "skipped"}[mutation])
    path = tmp_path / "report.xml"
    report.write(path)
    with pytest.raises(ValueError, match="pending search"):
        validate_pending_search_tests(path)
