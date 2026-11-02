"""Synthetic JUnit reports test CI's browser-skip rejection without a browser."""
from pathlib import Path
import runpy
import xml.etree.ElementTree as ET

import pytest


GATE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_browser_coverage.py"))


def report_fixture(tmp_path):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for module, names in GATE["REQUIRED_TESTS"].items():
        for name in sorted(names):
            ET.SubElement(suite, "testcase", classname=f"tests.{module}", name=name)
    return root, suite, tmp_path / "report.xml"


def test_complete_browser_coverage_allows_unrelated_windows_skips(tmp_path):
    root, suite, path = report_fixture(tmp_path)
    other = ET.SubElement(suite, "testcase", classname="tests.test_updater",
                          name="test_windows_only")
    ET.SubElement(other, "skipped")
    ET.ElementTree(root).write(path)
    assert GATE["audit_report"](path) == ([], 16)


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_browser_outcomes_are_not_silently_accepted(tmp_path, outcome):
    root, suite, path = report_fixture(tmp_path)
    case = suite[0]
    case.set("name", case.get("name") + "[fixture]")
    ET.SubElement(case, outcome)
    ET.ElementTree(root).write(path)
    errors, count = GATE["audit_report"](path)
    assert count == 16
    assert len(errors) == 1 and errors[0].endswith(": " + outcome)


def test_missing_browser_tests_fail_even_when_other_tests_pass(tmp_path):
    root, suite, path = report_fixture(tmp_path)
    suite.remove(suite[0])
    ET.ElementTree(root).write(path)
    errors, count = GATE["audit_report"](path)
    assert count == 15
    assert len(errors) == 1 and errors[0].endswith(": missing")


def test_all_parameterized_browser_cases_are_checked(tmp_path):
    root, suite, path = report_fixture(tmp_path)
    case = suite[0]
    duplicate = ET.SubElement(suite, "testcase", classname=case.get("classname"),
                              name=case.get("name") + "[second]")
    ET.SubElement(duplicate, "skipped")
    ET.ElementTree(root).write(path)
    errors, count = GATE["audit_report"](path)
    assert count == 17
    assert len(errors) == 1 and "[second]: skipped" in errors[0]
