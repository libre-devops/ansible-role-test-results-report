"""Unit tests for files/test_results_report.py: the contract, the policy, and the three formats.

Every report written here is validated against the published JSON Schema, so the schema and the
script cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("trr", ROOT / "files" / "test_results_report.py")
trr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trr)

REPORT_SCHEMA = json.loads((ROOT / "files" / "schemas" / "test-results-report.schema.json").read_text())
SUITE_SCHEMA = json.loads((ROOT / "files" / "schemas" / "test-results.schema.json").read_text())
NOW = dt.datetime(2026, 11, 15, 10, 0, tzinfo=dt.timezone.utc)


def check(check_id, status="passed", severity="medium", **extra):
    return {"id": check_id, "name": check_id.replace("_", " ").capitalize(), "status": status, "severity": severity, **extra}


def suite(checks, suite_id="demo_checks", **extra):
    return {"schema": trr.SUITE_SCHEMA, "suite": suite_id, "name": "Demo checks", "checks": checks, **extra}


def write_host(directory: Path, host, suites, label=None, facts=None):
    (directory / "hosts").mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": trr.HOST_SCHEMA,
        "host": host,
        "label": label or host,
        "collected": "2026-11-15T09:59:00Z",
        "facts": facts or {},
        "suites": suites,
    }
    (directory / "hosts" / f"{host}.json").write_text(json.dumps(payload))


def report_for(tmp_path, policy=None, expected_hosts=(), **request):
    outcome = trr.run(
        {"dir": str(tmp_path), "policy": policy or {}, "expected_hosts": list(expected_hosts), "title": "Demo", **request}, now=NOW
    )
    report = json.loads((tmp_path / "test-results.json").read_text())
    jsonschema.validate(report, REPORT_SCHEMA)
    return outcome, report


def result(report, check_id, host=None):
    return next(r for r in report["results"] if r["id"] == check_id and (host is None or r["host"] == host))


# --- The contract ------------------------------------------------------------------------------


def test_example_suite_matches_its_schema():
    jsonschema.validate(suite([check("a", metrics={"duration_seconds": 1.5}, detail="ok")]), SUITE_SCHEMA)


def test_all_passing_host_passes(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a"), check("b"), check("c", "skipped", detail="not applicable")])])
    outcome, report = report_for(tmp_path, expected_hosts=["vm1"])
    assert outcome["verdict"] == report["verdict"] == "passed"
    assert report["summary"].items() >= {"passed": 2, "skipped": 1, "ran": 2, "total": 3, "pass_rate": 100.0}.items()
    assert report["hosts"][0]["reasons"] == []


def test_defaults_fill_in_optional_fields(tmp_path):
    write_host(tmp_path, "vm1", [suite([{"id": "bare", "status": "passed"}])])
    _, report = report_for(tmp_path)
    bare = result(report, "bare")
    assert bare["name"] == "bare" and bare["severity"] == "medium"
    assert bare["detail"] is None and bare["metrics"] == {} and bare["limits"] == []


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"id": "has space", "name": "x", "status": "passed"}, "id must be"),
        ({"id": "x", "name": "x", "status": "ok"}, "status must be one of"),
        ({"id": "x", "name": "x", "status": "passed", "severity": "urgent"}, "severity must be one of"),
        ({"id": "x", "name": "x", "status": "passed", "metrics": {"m": "fast"}}, "metrics must map names to numbers"),
        ({"id": "x", "name": "x", "status": "passed", "metrics": {"m": True}}, "metrics must map names to numbers"),
    ],
)
def test_a_broken_check_becomes_one_critical_error_not_a_crash(tmp_path, bad, message):
    write_host(tmp_path, "vm1", [suite([check("fine"), bad])])
    _, report = report_for(tmp_path)
    assert [r["id"] for r in report["results"]] == ["invalid_results"]
    broken = report["results"][0]
    assert broken["outcome"] == "error" and broken["severity"] == "critical"
    assert message in broken["detail"]
    assert report["verdict"] == "failed"


def test_duplicate_check_ids_are_refused(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("same"), check("same")])])
    _, report = report_for(tmp_path)
    assert "appears twice" in report["results"][0]["detail"]


def test_a_suite_of_another_schema_version_is_refused(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a")]) | {"schema": "libre_devops.test_results/v2"}])
    _, report = report_for(tmp_path)
    assert "this report reads libre_devops.test_results/v1" in report["results"][0]["detail"]


def test_a_host_the_play_expected_but_that_sent_nothing_fails(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a")])])
    _, report = report_for(tmp_path, expected_hosts=["vm1", "vm2"])
    vm2 = next(h for h in report["hosts"] if h["host"] == "vm2")
    assert vm2["missing"] and vm2["verdict"] == "failed" and "No results" in vm2["reasons"][0]
    assert report["verdict"] == "failed" and "1 host failed (allowed 0): vm2." in report["reasons"]


def test_hosts_from_other_runs_in_the_directory_are_included(tmp_path):
    # terraform-ansible-role runs one playbook per host; each run only expects its own host.
    write_host(tmp_path, "vm1", [suite([check("a")])])
    write_host(tmp_path, "vm2", [suite([check("a", "failed")])])
    _, report = report_for(tmp_path, expected_hosts=["vm1"])
    assert [h["host"] for h in report["hosts"]] == ["vm1", "vm2"]
    assert report["verdict"] == "failed"


def test_no_hosts_at_all_fails(tmp_path):
    _, report = report_for(tmp_path)
    assert report["verdict"] == "failed" and report["reasons"] == ["No hosts reported results."]


def test_two_suites_on_one_host(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a")], "one"), suite([check("a", "failed")], "two")])
    _, report = report_for(tmp_path)
    assert [(r["suite"], r["outcome"]) for r in report["results"]] == [("one", "passed"), ("two", "failed")]
    assert "two/a" in report["hosts"][0]["reasons"][0]


# --- The policy --------------------------------------------------------------------------------


def test_fail_severity_turns_lower_failures_into_warnings(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("minor", "failed", "low"), check("note", "error", "info")])])
    _, report = report_for(tmp_path, {"fail_severity": "medium"})
    assert [r["outcome"] for r in report["results"]] == ["warning", "warning"]
    assert report["verdict"] == "passed"
    # The default counts everything from low up.
    _, report = report_for(tmp_path)
    assert [r["outcome"] for r in report["results"]] == ["failed", "warning"]


def test_max_failures_tolerates_some_failures_per_host(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a", "failed"), check("b", "error"), check("c")])])
    assert report_for(tmp_path, {"max_failures": 2})[1]["verdict"] == "passed"
    _, report = report_for(tmp_path, {"max_failures": 1})
    assert report["hosts"][0]["reasons"] == ["2 failures at low severity or above (allowed 1): a, b."]


def test_min_pass_rate(tmp_path):
    write_host(tmp_path, "vm1", [suite([check(f"c{n}") for n in range(9)] + [check("bad", "failed", "low")])])
    policy = {"max_failures": 5, "min_pass_rate": 95}
    _, report = report_for(tmp_path, policy)
    assert report["hosts"][0]["summary"]["pass_rate"] == 90.0
    assert "Pass rate 90% (at least 95% expected)." in report["hosts"][0]["reasons"]
    assert report_for(tmp_path, policy | {"min_pass_rate": 90})[1]["verdict"] == "passed"


def test_min_checks_fails_a_host_where_everything_was_skipped(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a", "skipped"), check("b", "skipped")])])
    _, report = report_for(tmp_path)
    assert report["hosts"][0]["reasons"] == ["0 checks ran (at least 1 expected)."]
    assert report_for(tmp_path, {"min_checks": 0})[1]["verdict"] == "passed"


def test_expected_checks_must_report_and_must_not_be_skipped(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("ran"), check("offline_only", "skipped", detail="no internet")])])
    _, report = report_for(tmp_path, {"expected_checks": ["ran", "offline_only", "demo_checks/absent", {"check": "x", "hosts": ["other"]}]})
    assert (
        report["hosts"][0]["reasons"]
        == [
            "Expected check demo_checks/absent did not report.",
            "Expected check offline_only was skipped: no internet",
        ][::-1]
    )


def test_max_failed_hosts(tmp_path):
    for host in ("vm1", "vm2", "vm3"):
        write_host(tmp_path, host, [suite([check("a", "failed" if host == "vm2" else "passed")])])
    _, report = report_for(tmp_path, {"max_failed_hosts": 1})
    assert report["verdict"] == "passed" and report["summary"]["hosts_failed"] == 1
    assert report_for(tmp_path)[1]["verdict"] == "failed"


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        ({"fail_severity": "severe"}, "fail_severity must be one of"),
        ({"max_failures": -1}, "max_failures must be 0 or more"),
        ({"max_failures": 1.5}, "must be a whole number"),
        ({"min_pass_rate": 101}, "0 to 100"),
        ({"waivers": [{"check": "a"}]}, "needs a reason"),
        ({"waivers": [{"check": "a", "reason": "x", "expires": "next week"}]}, "is not a date"),
        ({"limits": [{"check": "a", "metric": "m"}]}, "needs at least one of"),
        ({"limits": [{"check": "a", "max": 1}]}, "needs metric"),
        ({"surprise": 1}, "unknown settings: surprise"),
    ],
)
def test_a_wrong_policy_stops_the_report(tmp_path, policy, message):
    write_host(tmp_path, "vm1", [suite([check("a")])])
    with pytest.raises(trr.RequestError, match=message):
        trr.run({"dir": str(tmp_path), "policy": policy}, now=NOW)


# --- Waivers ------------------------------------------------------------------------------------


def test_a_waiver_accepts_a_failure_and_says_why(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("eicar", "failed", "critical", detail="not removed")])])
    _, report = report_for(tmp_path, {"waivers": [{"check": "eicar", "reason": "Passive mode in the lab", "reference": "CHG-1"}]})
    eicar = result(report, "eicar")
    assert eicar["outcome"] == "waived" and eicar["status"] == "failed"
    assert eicar["waiver"] == {"reason": "Passive mode in the lab", "reference": "CHG-1", "expires": None}
    assert report["verdict"] == "passed" and report["waivers"][0]["state"] == "active"
    assert report["summary"]["pass_rate"] == 100.0


def test_an_expired_waiver_is_not_applied_and_is_shown(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("eicar", "failed")])])
    _, report = report_for(tmp_path, {"waivers": [{"check": "eicar", "reason": "old", "expires": "2026-11-14"}]})
    eicar = result(report, "eicar")
    assert eicar["outcome"] == "failed" and eicar["expired_waiver"]["expires"] == "2026-11-14"
    assert report["waivers"][0]["state"] == "expired" and report["verdict"] == "failed"
    # A waiver is good up to and including its expiry date.
    _, report = report_for(tmp_path, {"waivers": [{"check": "eicar", "reason": "today", "expires": "2026-11-15"}]})
    assert result(report, "eicar")["outcome"] == "waived"


def test_an_unused_waiver_is_reported_as_unused(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("eicar")])])
    _, report = report_for(tmp_path, {"waivers": [{"check": "eicar", "reason": "flaky"}]})
    assert report["waivers"][0]["state"] == "unused"
    assert report["waivers"][0]["matched"] == 0


def test_waivers_select_by_wildcard_suite_and_host_or_label(tmp_path):
    write_host(tmp_path, "vm-a", [suite([check("upgrade_fields", "failed"), check("eicar", "failed")])], label="rhel8")
    write_host(tmp_path, "vm-b", [suite([check("upgrade_fields", "failed")])], label="rhel9")
    policy = {
        "waivers": [
            {"check": "upgrade_*", "hosts": ["rhel8"], "reason": "RHEL 8 lacks the field"},
            {"check": "eicar", "suite": "other_suite", "reason": "wrong suite, so no match"},
        ]
    }
    _, report = report_for(tmp_path, policy)
    assert result(report, "upgrade_fields", "vm-a")["outcome"] == "waived"
    assert result(report, "upgrade_fields", "vm-b")["outcome"] == "failed"
    assert result(report, "eicar")["outcome"] == "failed"


def test_a_waiver_does_not_hide_a_skip(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a"), check("b", "skipped")])])
    _, report = report_for(tmp_path, {"waivers": [{"check": "b", "reason": "x"}]})
    assert result(report, "b")["outcome"] == "skipped" and report["waivers"][0]["state"] == "unused"


# --- Limits -------------------------------------------------------------------------------------


def test_a_limit_fails_a_passed_check_at_the_limits_severity(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("quick_scan", severity="critical", metrics={"duration_seconds": 742.5})])])
    _, report = report_for(tmp_path, {"limits": [{"check": "quick_scan", "metric": "duration_seconds", "max": 600, "severity": "low"}]})
    scan = result(report, "quick_scan")
    assert scan["status"] == "failed" and scan["reported_status"] == "passed"
    assert scan["severity"] == "low" and scan["reported_severity"] == "critical"
    assert scan["limits"][0]["breaches"] == ["duration_seconds 742.5 is above the maximum 600"]
    assert "Limit: duration_seconds 742.5 is above the maximum 600." in scan["detail"]
    assert report["limits"][0].items() >= {"evaluated": 1, "breached": 1, "not_evaluated": 0}.items()


def test_a_limit_within_bounds_changes_nothing(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("mem", metrics={"memory_mib": 300})])])
    _, report = report_for(tmp_path, {"limits": [{"check": "mem", "metric": "memory_mib", "min": 100, "max": 500}]})
    mem = result(report, "mem")
    assert mem["outcome"] == "passed" and mem["limits"][0]["evaluated"] and mem["limits"][0]["value"] == 300


def test_a_limit_on_a_missing_metric_is_an_error_not_a_silent_pass(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("quick_scan"), check("skipped_scan", "skipped")])])
    limits = [{"check": "*scan", "metric": "duration_seconds", "max": 600}]
    _, report = report_for(tmp_path, {"limits": limits})
    assert result(report, "quick_scan")["status"] == "error"
    assert "reported no duration_seconds metric" in result(report, "quick_scan")["detail"]
    assert result(report, "skipped_scan")["outcome"] == "skipped"
    assert report["limits"][0]["not_evaluated"] == 1


def test_a_limit_only_raises_the_severity_of_a_check_that_failed_anyway(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("scan", "failed", "low", metrics={"duration_seconds": 900})])])
    limits = [{"check": "scan", "metric": "duration_seconds", "max": 600, "severity": "high"}]
    assert result(report_for(tmp_path, {"limits": limits})[1], "scan")["severity"] == "high"
    limits[0]["severity"] = "info"
    assert result(report_for(tmp_path, {"limits": limits})[1], "scan")["severity"] == "low"


def test_percentage_limits_compare_with_the_previous_report(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write_host(first, "vm-old", [suite([check("idle", metrics={"memory_mib": 400, "cpu_percent": 0})])], label="rhel9")
    report_for(first)
    write_host(second, "vm-new", [suite([check("idle", metrics={"memory_mib": 520, "cpu_percent": 0})])], label="rhel9")
    limits = [
        {"check": "idle", "metric": "memory_mib", "max_increase_percent": 20},
        {"check": "idle", "metric": "cpu_percent", "max_increase_percent": 20},
    ]
    _, report = report_for(second, {"limits": limits}, compare_to=str(first / "test-results.json"))
    idle = result(report, "idle")
    assert idle["status"] == "failed"
    memory = idle["limits"][0]
    assert memory["baseline"] == 400 and memory["change_percent"] == 30.0
    assert memory["breaches"] == ["memory_mib rose from 400 to 520 (more than the 20% allowed)"]
    assert idle["limits"][1]["breaches"] == []  # 0 to 0 is no change, not an infinite rise


def test_percentage_limits_without_a_previous_report_do_not_fail(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("idle", metrics={"memory_mib": 900})])])
    _, report = report_for(tmp_path, {"limits": [{"check": "idle", "metric": "memory_mib", "max_increase_percent": 1}]})
    assert result(report, "idle")["outcome"] == "passed"


# --- Comparison with an earlier report ---------------------------------------------------------


def test_comparison_by_label_finds_regressions_fixes_and_missing_checks(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write_host(first, "old-vm", [suite([check("a"), check("b", "failed"), check("gone"), check("same")])], label="rhel9")
    report_for(first, title="101.26091")
    write_host(second, "new-vm", [suite([check("a", "failed"), check("b"), check("same"), check("brand_new")])], label="rhel9")
    _, report = report_for(second, compare_to=str(first / "test-results.json"))
    changes = {r["id"]: r["change"] for r in report["results"]}
    assert changes == {"a": "regressed", "b": "fixed", "same": "unchanged", "brand_new": "new"}
    c = report["comparison"]
    assert (c["regressed"], c["fixed"], c["new"], c["unchanged"]) == (1, 1, 1, 1)
    assert c["missing"] == [{"label": "rhel9", "suite": "demo_checks", "id": "gone", "outcome": "passed"}]
    assert c["previous_title"] == "101.26091" and c["compared_to"].endswith("first/test-results.json")
    markdown = (second / "test-results.md").read_text()
    assert "## Changes since the previous report" in markdown and "**Not reported**" in markdown


def test_compare_to_must_be_a_report(tmp_path):
    (tmp_path / "other.json").write_text("{}")
    with pytest.raises(trr.RequestError, match="is not a libre_devops.test_results_report/v1 report"):
        trr.run({"dir": str(tmp_path), "compare_to": str(tmp_path / "other.json")}, now=NOW)


# --- The formats --------------------------------------------------------------------------------


def busy_host(tmp_path):
    write_host(
        tmp_path,
        "vm1",
        [
            suite(
                [
                    check("ok", detail="fine", duration_seconds=1.25),
                    check(
                        "bad",
                        "failed",
                        "high",
                        detail="a | b <c>",
                        expected="x",
                        actual="y",
                        remediation="fix it",
                        docs="https://example.org/doc",
                        output="line ```code```\n\x07bell",
                    ),
                    check("err", "error", detail="timed out"),
                    check("note", "failed", "info", detail="cosmetic"),
                    check("accepted", "failed", detail="known"),
                    check("skip", "skipped", detail="offline"),
                ],
                properties={"version": "1.2.3", "onboarded": False},
            )
        ],
        label="rhel9",
        facts={"distribution": "RedHat", "distribution_version": "9.6", "architecture": "x86_64", "kernel": "5.14"},
    )
    return {"waivers": [{"check": "accepted", "reason": "Ticket open", "reference": "INC-9"}], "fail_severity": "low"}


def test_junit_counts_only_what_counts_against_the_verdict(tmp_path):
    report_for(tmp_path, busy_host(tmp_path))
    root = ET.parse(tmp_path / "test-results.xml").getroot()
    assert root.tag == "testsuites"
    assert (root.get("tests"), root.get("failures"), root.get("errors"), root.get("skipped")) == ("6", "1", "1", "3")
    cases = {c.get("name").split(":")[0]: c for c in root.iter("testcase")}
    assert cases["bad"].find("failure").get("type") == "high"
    assert "\x07" not in cases["bad"].find("failure").text  # control characters cannot go in XML
    assert cases["err"].find("error") is not None
    assert cases["note"].find("skipped").get("message").startswith("Warning:")
    assert cases["accepted"].find("skipped").get("message").startswith("Waived: Ticket open (INC-9)")
    assert cases["skip"].find("skipped").get("message") == "offline"
    assert cases["ok"].get("classname") == "vm1.demo_checks" and cases["ok"].get("time") == "1.250"
    properties = {p.get("name"): p.get("value") for p in root.find("testsuite/properties")}
    assert properties["label"] == "rhel9" and properties["version"] == "1.2.3" and properties["onboarded"] == "false"


def test_junit_shows_a_host_without_results_as_an_error(tmp_path):
    report_for(tmp_path, expected_hosts=["gone"])
    root = ET.parse(tmp_path / "test-results.xml").getroot()
    assert root.find("testsuite/testcase/error").get("type") == "no_results"


def test_markdown_puts_the_verdict_and_what_needs_attention_first(tmp_path):
    report_for(tmp_path, busy_host(tmp_path))
    markdown = (tmp_path / "test-results.md").read_text()
    lines = markdown.splitlines()
    assert lines[0] == "# Demo"
    assert lines[2].startswith("**FAILED.** 0 of 1 host passed. 5 checks ran: 1 passed, 1 failed, 1 error, 1 warning, 1 waived; 1 skipped.")
    order = [
        markdown.index(h)
        for h in ("## Why it failed", "## Hosts", "## Needs attention", "## Accepted by a waiver", "## All results", "## How it was judged")
    ]
    assert order == sorted(order)
    # Severity order within a host: the high failure comes before the medium error.
    attention = markdown[markdown.index("## Needs attention") : markdown.index("## Accepted by a waiver")]
    assert attention.index("`bad`") < attention.index("`err`") < attention.index("`note`")


def test_markdown_escapes_table_cells_and_fences_output(tmp_path):
    report_for(tmp_path, busy_host(tmp_path))
    markdown = (tmp_path / "test-results.md").read_text()
    assert "a \\| b &lt;c&gt;" in markdown
    assert "````text\nline ```code```" in markdown  # the fence is longer than any run in the output
    assert "<c>" not in markdown


def test_markdown_template_can_be_replaced(tmp_path):
    template = tmp_path / "mine.md.j2"
    template.write_text("{{ report.verdict }} {{ report.summary.total }}\n")
    write_host(tmp_path, "vm1", [suite([check("a")])])
    trr.run({"dir": str(tmp_path), "markdown_template": str(template)}, now=NOW)
    assert (tmp_path / "test-results.md").read_text() == "passed 1\n"


def test_formats_and_basename(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("a")])])
    outcome = trr.run({"dir": str(tmp_path), "formats": ["junit"], "basename": "mdatp"}, now=NOW)
    assert set(outcome["files"]) == {"junit"} and (tmp_path / "mdatp.xml").exists()
    assert not (tmp_path / "mdatp.json").exists()


def test_long_output_is_cut_in_markdown_but_kept_in_json(tmp_path):
    write_host(tmp_path, "vm1", [suite([check("noisy", "failed", output="x" * 10000)])])
    _, report = report_for(tmp_path, output_limit=100)
    assert len(result(report, "noisy")["output"]) == 10000
    assert "(9900 more characters, cut)" in (tmp_path / "test-results.md").read_text()


def test_github_step_summary_is_replaced_not_appended(tmp_path):
    summary = tmp_path / "summary.md"
    summary.write_text("Earlier step output\n")
    write_host(tmp_path / "r", "vm1", [suite([check("a")])])
    request = {"dir": str(tmp_path / "r"), "github_step_summary": str(summary)}
    trr.run(request, now=NOW)
    trr.run(request, now=NOW)
    text = summary.read_text()
    assert text.startswith("Earlier step output\n")
    assert text.count("# Test results") == 1 and text.count("begin -->") == 1


def test_parallel_runs_sharing_a_directory_leave_a_complete_report(tmp_path):
    # Like terraform-ansible-role: one run per host, each writing its own host file first.
    hosts = [f"vm{n}" for n in range(8)]

    def one(host):
        write_host(tmp_path, host, [suite([check("a")])])
        trr.run({"dir": str(tmp_path), "expected_hosts": [host]}, now=NOW)

    threads = [threading.Thread(target=one, args=(h,)) for h in hosts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report = json.loads((tmp_path / "test-results.json").read_text())
    assert [h["host"] for h in report["hosts"]] == hosts


def test_console_summary(tmp_path):
    outcome, _ = report_for(tmp_path, busy_host(tmp_path))
    assert outcome["console"][0].startswith("FAILED: Demo. 0 of 1 host passed; 5 checks ran:")
    assert outcome["console"][1] == "1 host failed (allowed 0): vm1."
    assert outcome["console"][2].startswith("vm1: FAILED (1 passed, 1 failed, 1 error, 1 warning, 1 waived, 1 skipped). ")


def test_cli_reports_a_bad_request_on_stderr(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"dir": "relative/path"}))
    assert trr.main(["--request", str(request)]) == 2
    assert "dir must be an absolute path" in capsys.readouterr().err
