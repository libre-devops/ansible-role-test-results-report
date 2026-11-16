#!/usr/bin/env python3
"""Turn the test results that checks roles leave on each host into one report.

The role runs this on the control node with ansible_playbook_python, once per play, passing a
request as JSON on stdin. Each host's results are already in <dir>/hosts/<host>.json (the role
writes one file per host). Under a lock on the directory this script reads every host file, adds
the hosts the play expected but that sent nothing, applies the thresholds, limits and waivers,
and writes the report as JSON, Markdown and JUnit XML. It prints a short summary as JSON on
stdout for the role to show and act on.

Reading every host file in the directory, not only this play's hosts, is what lets several
ansible-playbook runs share one report: terraform-ansible-role runs one per host, in parallel,
and each run rebuilds the report from every host that has finished so far, so the last one to
finish leaves the complete report.

It needs only the Python standard library and Jinja2, which ansible-core itself depends on. It
can also be run by hand to rebuild a report from a directory of host files with another policy:
python3 test_results_report.py --request request.json
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import csv
import datetime as dt
import fcntl
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SUITE_SCHEMA = "libre_devops.test_results/v1"
HOST_SCHEMA = "libre_devops.test_results_host/v1"
REPORT_SCHEMA = "libre_devops.test_results_report/v1"
GENERATOR = "libre_devops.test_results_report"

STATUSES = ("passed", "failed", "error", "skipped")
OUTCOMES = ("passed", "failed", "error", "warning", "waived", "skipped")
# Lowest first: a check's severity is compared with the policy's fail_severity by position.
SEVERITIES = ("info", "low", "medium", "high", "critical")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
FORMATS = ("json", "markdown", "junit", "html", "csv")
DEFAULT_FORMATS = ("json", "html", "markdown", "junit")
CHANGES = ("new", "fixed", "regressed", "unchanged")

# Longest output kept per result in the JSON report. The Markdown report keeps less
# (output_limit in the request), so it stays readable.
JSON_OUTPUT_LIMIT = 65536
# GitHub refuses a job summary over 1 MiB, so a larger report goes in without "All results".
STEP_SUMMARY_LIMIT = 1_000_000

DEFAULT_POLICY: dict[str, Any] = {
    "fail_severity": "low",
    "max_failures": 0,
    "min_pass_rate": 0,
    "min_checks": 1,
    "expected_checks": [],
    "max_failed_hosts": 0,
    "waivers": [],
    "limits": [],
}


class RequestError(Exception):
    """The request itself is wrong: nothing can be reported, so the role stops."""


# --- Small helpers ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> dt.datetime | None:
    """A timestamp from a checks role or an earlier report, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def parse_date(value: Any, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise RequestError(f"{what}: {value!r} is not a date in the form YYYY-MM-DD") from exc


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def number(value: Any, what: str, minimum: float | None = None) -> float:
    if not is_number(value):
        raise RequestError(f"{what} must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise RequestError(f"{what} must be {minimum:g} or more, got {value!r}")
    return value


def text(value: Any) -> str | None:
    """Optional free text, as a string: JSON values other than strings are serialised."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def truncate(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return f"{value[:limit]}\n... ({len(value) - limit} more characters, cut)"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def locked(directory: Path):
    """Serialise report runs that share a directory (parallel per-host Terraform runs)."""
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- Selectors: which results a waiver, limit or expected check applies to -------------------


def parse_selector(raw: Any, what: str) -> dict[str, Any]:
    """A check selector: "check", "suite/check", or a mapping with check, suite and hosts.

    Every part takes shell-style wildcards (fnmatch), so "upgrade_*" or "*" work. hosts matches
    a host's inventory name or its label.
    """
    if isinstance(raw, str):
        suite, _, check = raw.rpartition("/")
        raw = {"check": check, "suite": suite or "*"}
    if not isinstance(raw, dict):
        raise RequestError(f"{what} must be a check name, suite/check, or a mapping, got {raw!r}")
    check = raw.get("check")
    if not isinstance(check, str) or not check:
        raise RequestError(f"{what} needs check: the check's id (wildcards allowed)")
    suite = raw.get("suite") or "*"
    hosts = raw.get("hosts") or ["*"]
    if isinstance(hosts, str):
        hosts = [hosts]
    if not isinstance(suite, str) or not all(isinstance(h, str) and h for h in hosts):
        raise RequestError(f"{what}: suite must be a string and hosts a list of host patterns")
    return {"check": check, "suite": suite, "hosts": list(hosts)}


def selector_matches(selector: dict[str, Any], host: dict[str, Any], suite: str, check: str) -> bool:
    return (
        fnmatch.fnmatchcase(check, selector["check"])
        and fnmatch.fnmatchcase(suite, selector["suite"])
        and any(fnmatch.fnmatchcase(host["host"], pattern) or fnmatch.fnmatchcase(host["label"], pattern) for pattern in selector["hosts"])
    )


def selector_text(selector: dict[str, Any]) -> str:
    name = selector["check"] if selector["suite"] == "*" else f"{selector['suite']}/{selector['check']}"
    if selector["hosts"] != ["*"]:
        name += f" on {', '.join(selector['hosts'])}"
    return name


# --- The policy -----------------------------------------------------------------------------


def normalise_policy(raw: Any, today: dt.date) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RequestError("policy must be a mapping")
    unknown = set(raw) - set(DEFAULT_POLICY)
    if unknown:
        raise RequestError(f"policy has unknown settings: {', '.join(sorted(unknown))}")
    policy = copy.deepcopy(DEFAULT_POLICY)
    policy.update({k: v for k, v in raw.items() if v is not None})

    if policy["fail_severity"] not in SEVERITIES:
        raise RequestError(f"fail_severity must be one of {', '.join(SEVERITIES)}")
    for key in ("max_failures", "min_checks", "max_failed_hosts"):
        value = number(policy[key], key, 0)
        if int(value) != value:
            raise RequestError(f"{key} must be a whole number, got {value!r}")
        policy[key] = int(value)
    policy["min_pass_rate"] = number(policy["min_pass_rate"], "min_pass_rate", 0)
    if policy["min_pass_rate"] > 100:
        raise RequestError("min_pass_rate is a percentage: 0 to 100")

    policy["expected_checks"] = [
        parse_selector(item, f"expected_checks[{index}]") for index, item in enumerate(policy["expected_checks"] or [])
    ]

    waivers = []
    for index, item in enumerate(policy["waivers"] or []):
        what = f"waivers[{index}]"
        selector = parse_selector(item, what)
        if not isinstance(item, dict) or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise RequestError(f"{what} needs a reason: say why the failure is accepted, so the report can show it")
        expires = parse_date(item["expires"], f"{what}.expires") if item.get("expires") else None
        waivers.append(
            {
                **selector,
                "reason": item["reason"].strip(),
                "reference": text(item.get("reference")),
                "expires": expires.isoformat() if expires else None,
                "state": "expired" if expires and expires < today else "active",
                "matched": 0,
            }
        )
    policy["waivers"] = waivers

    limits = []
    bounds = ("min", "max", "max_increase_percent", "max_decrease_percent")
    for index, item in enumerate(policy["limits"] or []):
        what = f"limits[{index}]"
        selector = parse_selector(item, what)
        metric = item.get("metric") if isinstance(item, dict) else None
        if not isinstance(metric, str) or not metric:
            raise RequestError(f"{what} needs metric: the name of a metric the check reports")
        limit = {**selector, "metric": metric}
        for bound in bounds:
            limit[bound] = number(item[bound], f"{what}.{bound}") if item.get(bound) is not None else None
        if all(limit[bound] is None for bound in bounds):
            raise RequestError(f"{what} needs at least one of {', '.join(bounds)}")
        severity = item.get("severity")
        if severity is not None and severity not in SEVERITIES:
            raise RequestError(f"{what}.severity must be one of {', '.join(SEVERITIES)}")
        limit["severity"] = severity
        limit.update({"evaluated": 0, "breached": 0, "not_evaluated": 0})
        limits.append(limit)
    policy["limits"] = limits
    return policy


# --- Reading what the checks roles reported ---------------------------------------------------


def invalid_suite_result(suite_id: str, problem: str) -> dict[str, Any]:
    """A suite that broke the contract still shows up, as an error, rather than vanishing."""
    return {
        "id": "invalid_results",
        "name": f"{suite_id} reported results this report can read",
        "status": "error",
        "severity": "critical",
        "category": "report",
        "detail": problem,
        "remediation": "Fix the checks role so it reports results in the documented format "
        "(see the test_results_report README, Writing a checks role).",
    }


def normalise_check(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} is not a mapping")
    check_id = raw.get("id")
    if not isinstance(check_id, str) or not ID_PATTERN.match(check_id):
        raise ValueError(f"{where}.id must be letters, digits, _, . or -, got {check_id!r}")
    status = raw.get("status")
    if status not in STATUSES:
        raise ValueError(f"{where} ({check_id}): status must be one of {', '.join(STATUSES)}, got {status!r}")
    severity = raw.get("severity", "medium")
    if severity not in SEVERITIES:
        raise ValueError(f"{where} ({check_id}): severity must be one of {', '.join(SEVERITIES)}, got {severity!r}")
    metrics = raw.get("metrics") or {}
    if not isinstance(metrics, dict) or not all(isinstance(k, str) and is_number(v) for k, v in metrics.items()):
        raise ValueError(f"{where} ({check_id}): metrics must map names to numbers")
    duration = raw.get("duration_seconds")
    if duration is not None and not (is_number(duration) and duration >= 0):
        raise ValueError(f"{where} ({check_id}): duration_seconds must be a number, 0 or more")
    name = raw.get("name") or check_id
    return {
        "id": check_id,
        "name": str(name),
        "status": status,
        "severity": severity,
        "category": text(raw.get("category")),
        "detail": text(raw.get("detail")),
        "expected": text(raw.get("expected")),
        "actual": text(raw.get("actual")),
        "remediation": text(raw.get("remediation")),
        "docs": text(raw.get("docs")),
        "output": truncate(text(raw.get("output")), JSON_OUTPUT_LIMIT),
        "metrics": {k: metrics[k] for k in sorted(metrics)},
        "duration_seconds": duration,
    }


def normalise_suite(raw: Any, index: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the suite's details and its checks. A broken suite becomes one error result."""
    suite_id = raw.get("suite") if isinstance(raw, dict) else None
    if not isinstance(suite_id, str) or not ID_PATTERN.match(suite_id):
        suite_id = f"suite_{index + 1}"
        return (
            {"suite": suite_id, "name": suite_id, "started": None, "finished": None, "properties": {}},
            [normalise_check(invalid_suite_result(suite_id, "The suite has no valid suite id."), "suite")],
        )
    started, finished = parse_time(raw.get("started")), parse_time(raw.get("finished"))
    details = {
        "suite": suite_id,
        "name": str(raw.get("name") or suite_id),
        "started": iso(started) if started else None,
        "finished": iso(finished) if finished else None,
        "properties": raw.get("properties") if isinstance(raw.get("properties"), dict) else {},
    }
    try:
        schema = raw.get("schema", SUITE_SCHEMA)
        if schema != SUITE_SCHEMA:
            raise ValueError(f"schema is {schema!r}; this report reads {SUITE_SCHEMA}")
        checks_raw = raw.get("checks")
        if not isinstance(checks_raw, list):
            raise ValueError("checks must be a list")
        checks = [normalise_check(item, f"checks[{n}]") for n, item in enumerate(checks_raw)]
        seen: set[str] = set()
        for check in checks:
            if check["id"] in seen:
                raise ValueError(f"check id {check['id']} appears twice, so waivers and limits cannot tell them apart")
            seen.add(check["id"])
    except ValueError as exc:
        return details, [normalise_check(invalid_suite_result(suite_id, str(exc)), "suite")]
    return details, checks


def read_hosts(directory: Path, expected_hosts: list[str]) -> list[dict[str, Any]]:
    """Every host file in the directory, plus an empty entry for each expected host without one."""
    hosts: dict[str, dict[str, Any]] = {}
    for path in sorted((directory / "hosts").glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RequestError(f"{path} is not readable JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != HOST_SCHEMA or not isinstance(raw.get("host"), str):
            raise RequestError(f"{path} is not a host results file ({HOST_SCHEMA})")
        hosts[raw["host"]] = raw
    for name in expected_hosts:
        hosts.setdefault(name, {"schema": HOST_SCHEMA, "host": name, "label": name, "missing": True, "suites": []})
    return [hosts[name] for name in sorted(hosts)]


# --- Applying the policy ---------------------------------------------------------------------


def apply_limits(result: dict[str, Any], host: dict[str, Any], policy: dict[str, Any], previous: dict | None) -> None:
    """Check the result's metrics against the limits that select it.

    A limit the check cannot be measured against (it ran but reported no such metric) is an
    error, not a silent pass: a limit that stops being enforced after an upgrade must show.
    """
    result["limits"] = []
    for limit in policy["limits"]:
        if not selector_matches(limit, host, result["suite"], result["id"]):
            continue
        entry = {
            "metric": limit["metric"],
            "value": None,
            "baseline": None,
            "change_percent": None,
            "severity": limit["severity"],
            "breaches": [],
            "evaluated": False,
        }
        for bound in ("min", "max", "max_increase_percent", "max_decrease_percent"):
            entry[bound] = limit[bound]
        result["limits"].append(entry)
        if result["status"] == "skipped":
            continue
        value = result["metrics"].get(limit["metric"])
        if value is None:
            limit["not_evaluated"] += 1
            entry["breaches"].append(f"the check reported no {limit['metric']} metric, so the limit could not be checked")
            continue
        entry["value"] = value
        entry["evaluated"] = True
        limit["evaluated"] += 1
        if limit["min"] is not None and value < limit["min"]:
            entry["breaches"].append(f"{limit['metric']} {value:g} is below the minimum {limit['min']:g}")
        if limit["max"] is not None and value > limit["max"]:
            entry["breaches"].append(f"{limit['metric']} {value:g} is above the maximum {limit['max']:g}")
        baseline = (previous or {}).get("metrics", {}).get(limit["metric"])
        if is_number(baseline):
            entry["baseline"] = baseline
            change = ((value - baseline) / abs(baseline) * 100) if baseline else (0.0 if value == baseline else math.inf)
            entry["change_percent"] = round(change, 2) if math.isfinite(change) else None
            if limit["max_increase_percent"] is not None and change > limit["max_increase_percent"]:
                entry["breaches"].append(
                    f"{limit['metric']} rose from {baseline:g} to {value:g} (more than the {limit['max_increase_percent']:g}% allowed)"
                )
            if limit["max_decrease_percent"] is not None and -change > limit["max_decrease_percent"]:
                entry["breaches"].append(
                    f"{limit['metric']} fell from {baseline:g} to {value:g} (more than the {limit['max_decrease_percent']:g}% allowed)"
                )
        if entry["breaches"] and entry["evaluated"]:
            limit["breached"] += 1

    broken = [entry for entry in result["limits"] if entry["breaches"]]
    if not broken:
        return
    if result["status"] == "passed":
        # A limit alone failed the check: a measured breach is a failure, a limit that could not
        # be measured is an error. The limit's own severity, when it has one, is what counts.
        result["status"] = "failed" if any(entry["evaluated"] for entry in broken) else "error"
        severities = [entry["severity"] for entry in broken if entry["severity"]]
        if severities:
            result["severity"] = max(severities, key=severity_rank)
    else:
        # The check failed by itself too: a limit can only make it count for more.
        result["severity"] = max([result["severity"], *(e["severity"] for e in broken if e["severity"])], key=severity_rank)
    note = "Limit: " + "; ".join(breach for entry in broken for breach in entry["breaches"]) + "."
    result["detail"] = f"{result['detail']} {note}" if result["detail"] else note


def classify(result: dict[str, Any], host: dict[str, Any], policy: dict[str, Any]) -> None:
    """Set the result's outcome: what it means for the verdict once the policy is applied."""
    result["waiver"] = None
    result["expired_waiver"] = None
    status = result["status"]
    if status in ("passed", "skipped"):
        result["outcome"] = status
        return
    for waiver in policy["waivers"]:
        if waiver["state"] == "expired" or not selector_matches(waiver, host, result["suite"], result["id"]):
            continue
        waiver["matched"] += 1
        result["waiver"] = {k: waiver[k] for k in ("reason", "reference", "expires")}
        result["outcome"] = "waived"
        return
    expired = [w for w in policy["waivers"] if w["state"] == "expired" and selector_matches(w, host, result["suite"], result["id"])]
    if expired:
        for waiver in expired:
            waiver["matched"] += 1
        result["expired_waiver"] = {k: expired[0][k] for k in ("reason", "reference", "expires")}
    if severity_rank(result["severity"]) < severity_rank(policy["fail_severity"]):
        result["outcome"] = "warning"
    else:
        result["outcome"] = status


def count(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {outcome: 0 for outcome in OUTCOMES}
    for result in results:
        summary[result["outcome"]] += 1
    summary["total"] = len(results)
    summary["ran"] = len(results) - summary["skipped"]
    summary["pass_rate"] = round((summary["passed"] + summary["waived"]) / summary["ran"] * 100, 2) if summary["ran"] else None
    return summary


def judge_host(host: dict[str, Any], results: list[dict[str, Any]], policy: dict[str, Any]) -> list[str]:
    """The reasons a host fails the gate; none means it passed."""
    if host.get("missing"):
        return ["No results: the host was unreachable, failed before the checks ran, or ran no checks role."]
    reasons = []
    summary = host["summary"]
    counted = [r for r in results if r["outcome"] in ("failed", "error")]
    if len(counted) > policy["max_failures"]:
        floor = policy["fail_severity"]
        scope = "" if floor == SEVERITIES[0] else f" at {floor} severity or above"
        names = ", ".join(f"{r['suite']}/{r['id']}" if len({x["suite"] for x in results}) > 1 else r["id"] for r in counted)
        reasons.append(f"{plural(len(counted), 'failure')}{scope} (allowed {policy['max_failures']}): {names}.")
    if summary["ran"] < policy["min_checks"]:
        reasons.append(f"{plural(summary['ran'], 'check')} ran (at least {policy['min_checks']} expected).")
    if policy["min_pass_rate"] and summary["pass_rate"] is not None and summary["pass_rate"] < policy["min_pass_rate"]:
        reasons.append(f"Pass rate {summary['pass_rate']:g}% (at least {policy['min_pass_rate']:g}% expected).")
    for selector in policy["expected_checks"]:
        if not any(fnmatch.fnmatchcase(host["host"], p) or fnmatch.fnmatchcase(host["label"], p) for p in selector["hosts"]):
            continue
        matching = [
            r for r in results if fnmatch.fnmatchcase(r["id"], selector["check"]) and fnmatch.fnmatchcase(r["suite"], selector["suite"])
        ]
        if not matching:
            reasons.append(f"Expected check {selector_text(selector)} did not report.")
        elif all(r["status"] == "skipped" for r in matching):
            why = matching[0]["detail"] or "no reason given"
            reasons.append(f"Expected check {selector_text(selector)} was skipped: {why}")
    return reasons


def previous_results(previous_report: dict | None) -> dict[tuple[str, str, str], dict]:
    """An earlier report's results by host label, suite and check id."""
    earlier = {}
    for item in (previous_report or {}).get("results", []):
        if isinstance(item, dict) and {"label", "suite", "id"} <= set(item):
            earlier[(item["label"], item["suite"], item["id"])] = item
    return earlier


def compare(results: list[dict[str, Any]], previous_report: dict | None) -> dict | None:
    """Say how each result changed since an earlier report, matched by host label, suite and id.

    Labels, not inventory names, are what match: a release test builds new VMs each time, so
    give hosts a stable label (rhel9, say) with test_results_report_host_label.
    """
    if previous_report is None:
        for result in results:
            result["previous"] = None
            result["change"] = None
        return None
    earlier = previous_results(previous_report)
    seen = set()
    tally = {change: 0 for change in CHANGES}
    for result in results:
        key = (result["label"], result["suite"], result["id"])
        seen.add(key)
        before = earlier.get(key)
        if before is None:
            result["previous"] = None
            result["change"] = "new"
        else:
            result["previous"] = {
                "outcome": before.get("outcome"),
                "status": before.get("status"),
                "metrics": before.get("metrics") or {},
            }
            was_good = before.get("outcome") in ("passed", "waived")
            is_good = result["outcome"] in ("passed", "waived")
            was_bad = before.get("outcome") in ("failed", "error", "warning")
            is_bad = result["outcome"] in ("failed", "error", "warning")
            if was_bad and is_good:
                result["change"] = "fixed"
            elif was_good and is_bad:
                result["change"] = "regressed"
            else:
                result["change"] = "unchanged"
        tally[result["change"]] += 1
    labels_now = {r["label"] for r in results}
    missing = [
        {"label": label, "suite": suite, "id": check_id, "outcome": item.get("outcome")}
        for (label, suite, check_id), item in sorted(earlier.items())
        if (label, suite, check_id) not in seen and label in labels_now
    ]
    return {
        "compared_to": None,
        "previous_title": previous_report.get("title"),
        "previous_generated": previous_report.get("generated"),
        **tally,
        "missing": missing,
    }


def build_report(
    request: dict[str, Any], hosts_raw: list[dict[str, Any]], previous_report: dict | None, now: dt.datetime
) -> dict[str, Any]:
    policy = normalise_policy(request.get("policy"), now.date())

    hosts, suites, results = [], [], []
    for raw in hosts_raw:
        host = {
            "host": raw["host"],
            "label": str(raw.get("label") or raw["host"]),
            "collected": raw.get("collected"),
            "facts": raw.get("facts") if isinstance(raw.get("facts"), dict) else {},
            "missing": bool(raw.get("missing")),
        }
        host_results = []
        for index, suite_raw in enumerate(raw.get("suites") or []):
            details, checks = normalise_suite(suite_raw, index)
            started, finished = parse_time(details["started"]), parse_time(details["finished"])
            if started and finished and finished >= started:
                duration = (finished - started).total_seconds()
            else:
                duration = sum(c["duration_seconds"] or 0 for c in checks)
            suites.append({"host": host["host"], "label": host["label"], **details, "duration_seconds": duration})
            for check in checks:
                host_results.append({"host": host["host"], "label": host["label"], "suite": details["suite"], **check})
        hosts.append((host, host_results))

    # The earlier report is read before the limits run, so relative limits can use its metrics.
    earlier = previous_results(previous_report)
    for host, host_results in hosts:
        for result in host_results:
            result["reported_status"] = result["status"]
            result["reported_severity"] = result["severity"]
            apply_limits(result, host, policy, earlier.get((result["label"], result["suite"], result["id"])))
            classify(result, host, policy)
        host["summary"] = count(host_results)
        host["reasons"] = judge_host(host, host_results, policy)
        host["verdict"] = "failed" if host["reasons"] else "passed"
        results.extend(host_results)

    comparison = compare(results, previous_report)

    failed_hosts = [h for h, _ in hosts if h["verdict"] == "failed"]
    reasons = []
    if not hosts:
        reasons.append("No hosts reported results.")
    if len(failed_hosts) > policy["max_failed_hosts"]:
        reasons.append(
            f"{plural(len(failed_hosts), 'host')} failed (allowed {policy['max_failed_hosts']}): "
            f"{', '.join(h['host'] for h in failed_hosts)}."
        )
    summary = count(results)
    summary.update({"hosts": len(hosts), "hosts_passed": len(hosts) - len(failed_hosts), "hosts_failed": len(failed_hosts)})

    for waiver in policy["waivers"]:
        if waiver["state"] == "active" and waiver["matched"] == 0:
            waiver["state"] = "unused"

    return {
        "schema": REPORT_SCHEMA,
        "generator": GENERATOR,
        "title": str(request.get("title") or "Test results"),
        "generated": iso(now),
        "verdict": "failed" if reasons else "passed",
        "reasons": reasons,
        "summary": summary,
        "properties": request.get("properties") if isinstance(request.get("properties"), dict) else {},
        "policy": {k: policy[k] for k in ("fail_severity", "max_failures", "min_pass_rate", "min_checks", "max_failed_hosts")}
        | {"expected_checks": [selector_text(s) for s in policy["expected_checks"]]},
        "waivers": policy["waivers"],
        "limits": policy["limits"],
        "comparison": comparison,
        "hosts": [host for host, _ in hosts],
        "suites": suites,
        "results": results,
    }


# --- Writing the report ----------------------------------------------------------------------


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def xml_text(value: Any) -> str:
    """XML 1.0 cannot carry most control characters, which command output sometimes has."""
    return _XML_INVALID.sub("�", "" if value is None else str(value))


def render_junit(report: dict[str, Any]) -> str:
    """JUnit XML for CI test views: one testsuite per host and suite, one testcase per check.

    Only failures that count against the verdict are <failure> or <error>, so a CI tool that
    fails the build on test failures agrees with the report. A waived failure and a warning are
    <skipped> with a message saying which, and plain skipped checks are <skipped> too.
    """
    summary = report["summary"]
    root = ET.Element(
        "testsuites",
        name=xml_text(report["title"]),
        tests=str(summary["total"]),
        failures=str(summary["failed"]),
        errors=str(summary["error"]),
        skipped=str(summary["skipped"] + summary["waived"] + summary["warning"]),
        time=f"{sum(s['duration_seconds'] for s in report['suites']):.3f}",
        timestamp=report["generated"],
    )
    for host in report["hosts"]:
        host_suites = [s for s in report["suites"] if s["host"] == host["host"]]
        if not host_suites:
            # A host with no results is an error the CI view must show, not an empty suite.
            element = ET.SubElement(
                root,
                "testsuite",
                name=xml_text(f"{host['host']}: no results"),
                tests="1",
                failures="0",
                errors="1",
                skipped="0",
                time="0.000",
                timestamp=report["generated"],
                hostname=xml_text(host["host"]),
            )
            case = ET.SubElement(element, "testcase", classname=xml_text(host["host"]), name="results reported", time="0.000")
            ET.SubElement(case, "error", message=xml_text(" ".join(host["reasons"])), type="no_results")
            continue
        for suite in host_suites:
            results = [r for r in report["results"] if r["host"] == host["host"] and r["suite"] == suite["suite"]]
            tally = count(results)
            element = ET.SubElement(
                root,
                "testsuite",
                name=xml_text(f"{host['host']}: {suite['name']}"),
                tests=str(len(results)),
                failures=str(tally["failed"]),
                errors=str(tally["error"]),
                skipped=str(tally["skipped"] + tally["waived"] + tally["warning"]),
                time=f"{suite['duration_seconds']:.3f}",
                timestamp=suite["started"] or report["generated"],
                hostname=xml_text(host["host"]),
            )
            properties = ET.SubElement(element, "properties")
            for key, value in [("label", host["label"]), *sorted(host["facts"].items()), *sorted(suite["properties"].items())]:
                ET.SubElement(properties, "property", name=xml_text(key), value=xml_text(text(value)))
            for result in results:
                case = ET.SubElement(
                    element,
                    "testcase",
                    classname=xml_text(f"{host['host']}.{suite['suite']}"),
                    name=xml_text(f"{result['id']}: {result['name']}"),
                    time=f"{result['duration_seconds'] or 0:.3f}",
                )
                case_properties = ET.SubElement(case, "properties")
                for key in ("id", "severity", "status", "outcome", "category"):
                    if result.get(key) is not None:
                        ET.SubElement(case_properties, "property", name=key, value=xml_text(result[key]))
                for metric, value in result["metrics"].items():
                    ET.SubElement(case_properties, "property", name=f"metric.{metric}", value=f"{value:g}")
                body = "\n".join(
                    f"{label}: {result[key]}"
                    for label, key in (
                        ("Detail", "detail"),
                        ("Expected", "expected"),
                        ("Actual", "actual"),
                        ("Remediation", "remediation"),
                        ("Docs", "docs"),
                    )
                    if result.get(key)
                )
                if result.get("output"):
                    body += f"\n\n{result['output']}"
                outcome = result["outcome"]
                if outcome in ("failed", "error"):
                    tag = "failure" if outcome == "failed" else "error"
                    node = ET.SubElement(case, tag, message=xml_text(result["detail"] or outcome), type=result["severity"])
                    node.text = xml_text(body)
                elif outcome == "waived":
                    waiver = result["waiver"]
                    note = f"Waived: {waiver['reason']}" + (f" ({waiver['reference']})" if waiver.get("reference") else "")
                    ET.SubElement(case, "skipped", message=xml_text(f"{note}. Was {result['status']}: {result['detail'] or ''}".strip()))
                elif outcome == "warning":
                    ET.SubElement(
                        case,
                        "skipped",
                        message=xml_text(
                            f"Warning: {result['status']} at {result['severity']} severity, below the "
                            f"{report['policy']['fail_severity']} threshold: {result['detail'] or ''}".strip()
                        ),
                    )
                elif outcome == "skipped":
                    ET.SubElement(case, "skipped", message=xml_text(result["detail"] or "skipped"))
                if body and outcome in ("passed",):
                    ET.SubElement(case, "system-out").text = xml_text(body)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def md_cell(value: Any) -> str:
    """Text that stays inside one Markdown table cell and is never read as HTML."""
    if value is None or value == "":
        return ""
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\s*\n\s*", " ", value).replace("|", "\\|").strip()


def md_text(value: Any) -> str:
    """Text outside tables: no HTML, but line breaks kept."""
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_code(value: Any) -> str:
    value = "" if value is None else str(value)
    fence = "`" * max(1, max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def md_block(value: Any) -> str:
    value = "" if value is None else str(value).rstrip("\n")
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`{3,}", value)), default=0) + 1)
    return f"{fence}text\n{value}\n{fence}"


def needing_attention(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Failures, errors and warnings, by host, then severity (highest first), then outcome."""
    severity_order = {severity: index for index, severity in enumerate(reversed(SEVERITIES))}
    attention = [r for r in report["results"] if r["outcome"] in ("failed", "error", "warning")]
    attention.sort(key=lambda r: (r["host"], severity_order[r["severity"]], ("failed", "error", "warning").index(r["outcome"])))
    return attention


def render_markdown(report: dict[str, Any], template_path: Path, output_limit: int, compact: bool = False) -> str:
    import jinja2  # ansible-core depends on it, so it is always there under ansible_playbook_python

    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    environment.filters.update(
        {
            "cell": md_cell,
            "text": md_text,
            "code": md_code,
            "block": md_block,
            "plural": lambda n, singular, plural_form=None: plural(n, singular, plural_form),
            "cut": lambda value: truncate(value, output_limit),
            "number": lambda value: "" if value is None else f"{value:g}",
        }
    )
    template = environment.get_template(template_path.name)
    content = template.render(
        report=report,
        attention=needing_attention(report),
        compact=compact,
        severities=list(reversed(SEVERITIES)),
        outcome_label={
            "passed": "Passed",
            "failed": "**Failed**",
            "error": "**Error**",
            "warning": "Warning",
            "waived": "Waived",
            "skipped": "Skipped",
        },
    )
    return re.sub(r"\n{3,}", "\n\n", content)


HTML_ASSETS = Path(__file__).resolve().parent / "html"


def csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def render_html(report: dict[str, Any], template_path: Path, output_limit: int) -> str:
    """One self-contained page: the stylesheet and script inline, nothing fetched.

    The content security policy lets only the page's own style and script run, by their
    hashes, and loads nothing at all: the page opens offline, from an email attachment or
    behind a proxy that blocks every CDN, and a value in the results cannot add a script.
    Autoescaping is on, so every value from the results is escaped.
    """
    import jinja2

    style = (HTML_ASSETS / "report.css").read_text(encoding="utf-8")
    script = (HTML_ASSETS / "report.js").read_text(encoding="utf-8")
    policy = (
        f"default-src 'none'; style-src {csp_hash(style)}; script-src {csp_hash(script)}; img-src data:; "
        "base-uri 'none'; form-action 'none'"
    )
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=True,
    )
    environment.filters.update(
        {
            "plural": lambda n, singular, plural_form=None: plural(n, singular, plural_form),
            "cut": lambda value: truncate(value, output_limit),
            "number": lambda value: "" if value is None else f"{value:g}",
        }
    )
    # Only web links become links: a javascript: or data: value in the results stays text.
    environment.tests["link"] = lambda value: isinstance(value, str) and re.match(r"^https?://[^\s\"'<>]+$", value) is not None
    content = environment.get_template(template_path.name).render(
        report=report, attention=needing_attention(report), style=style, script=script, policy=policy
    )
    # Blank lines between tags only: escaped output inside <pre> holds no raw < or >, so its own stay.
    return re.sub(r">\n{2,}<", ">\n<", content)


CSV_COLUMNS = (
    "host",
    "label",
    "suite",
    "id",
    "name",
    "category",
    "severity",
    "status",
    "outcome",
    "detail",
    "expected",
    "actual",
    "remediation",
    "docs",
    "metrics",
    "duration_seconds",
    "waiver",
    "change",
)


def render_csv(report: dict[str, Any]) -> str:
    """One row per result, for a spreadsheet: the columns people filter and pivot on."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for result in report["results"]:
        row = dict(result)
        row["metrics"] = "; ".join(f"{k}={v:g}" for k, v in result["metrics"].items())
        row["waiver"] = (result["waiver"] or {}).get("reason") or ""
        writer.writerow(["" if row.get(column) is None else row[column] for column in CSV_COLUMNS])
    return buffer.getvalue()


def update_step_summary(path: Path, key: str, markdown: str) -> None:
    """Put the report in the GitHub job summary, replacing this report's earlier copy.

    Several runs in one job step (one per host under Terraform) each rebuild the report, so each
    replaces the block the previous one wrote instead of adding another copy.
    """
    begin, end = f"<!-- {GENERATOR} {key} begin -->", f"<!-- {GENERATOR} {key} end -->"
    block = f"{begin}\n{markdown.rstrip()}\n{end}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    updated = pattern.sub(lambda _: block, existing, count=1) if pattern.search(existing) else existing + block
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(updated)


def console_lines(report: dict[str, Any], files: dict[str, str]) -> list[str]:
    summary = report["summary"]
    lines = [
        f"{report['verdict'].upper()}: {report['title']}. {summary['hosts_passed']} of "
        f"{plural(summary['hosts'], 'host')} passed; {plural(summary['ran'], 'check')} ran: {summary['passed']} passed, "
        f"{summary['failed']} failed, {plural(summary['error'], 'error')}, {plural(summary['warning'], 'warning')}, "
        f"{summary['waived']} waived, {summary['skipped']} skipped."
    ]
    lines += report["reasons"]
    for host in report["hosts"]:
        tally = host["summary"]
        state = "passed" if host["verdict"] == "passed" else "FAILED"
        line = (
            f"{host['host']}: {state} ({tally['passed']} passed, {tally['failed']} failed, {plural(tally['error'], 'error')}, "
            f"{plural(tally['warning'], 'warning')}, {tally['waived']} waived, {tally['skipped']} skipped)"
        )
        lines.append(line + (f". {' '.join(host['reasons'])}" if host["reasons"] else ""))
    for name in ("html", "markdown", "json", "junit", "csv"):
        if name in files:
            lines.append(f"{name}: {files[name]}")
    return lines


def run(request: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RequestError("the request must be a JSON object")
    directory = Path(request.get("dir") or "").expanduser()
    if not str(directory) or not directory.is_absolute():
        raise RequestError("dir must be an absolute path")
    formats = request.get("formats", list(DEFAULT_FORMATS))
    if not isinstance(formats, list) or not set(formats) <= set(FORMATS):
        raise RequestError(f"formats must be a list of {', '.join(FORMATS)}")
    basename = request.get("basename") or "test-results"
    if not isinstance(basename, str) or not ID_PATTERN.match(basename):
        raise RequestError("basename must be a plain file name without an extension")
    expected_hosts = request.get("expected_hosts") or []
    if not isinstance(expected_hosts, list) or not all(isinstance(h, str) for h in expected_hosts):
        raise RequestError("expected_hosts must be a list of host names")
    templates = Path(__file__).resolve().parent.parent / "templates"
    template_path = Path(request.get("markdown_template") or templates / "test-results.md.j2")
    if "markdown" in formats and not template_path.is_file():
        raise RequestError(f"markdown_template {template_path} does not exist")
    html_template = Path(request.get("html_template") or templates / "test-results.html.j2")
    if "html" in formats and not html_template.is_file():
        raise RequestError(f"html_template {html_template} does not exist")
    output_limit = int(request.get("output_limit") or 4000)
    now = now or (parse_time(request.get("now")) if request.get("now") else None) or utc_now()

    previous_report = None
    compare_to = request.get("compare_to")
    if compare_to:
        try:
            previous_report = json.loads(Path(compare_to).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RequestError(f"compare_to {compare_to} is not a readable report: {exc}") from exc
        if not isinstance(previous_report, dict) or previous_report.get("schema") != REPORT_SCHEMA:
            raise RequestError(f"compare_to {compare_to} is not a {REPORT_SCHEMA} report")

    files: dict[str, str] = {}
    with locked(directory):
        report = build_report(request, read_hosts(directory, expected_hosts), previous_report, now)
        if report["comparison"] is not None:
            report["comparison"]["compared_to"] = str(compare_to)
        if "json" in formats:
            files["json"] = str(directory / f"{basename}.json")
            atomic_write(Path(files["json"]), render_json(report))
        markdown = None
        if "markdown" in formats:
            markdown = render_markdown(report, template_path, output_limit)
            files["markdown"] = str(directory / f"{basename}.md")
            atomic_write(Path(files["markdown"]), markdown)
        if "junit" in formats:
            files["junit"] = str(directory / f"{basename}.xml")
            atomic_write(Path(files["junit"]), render_junit(report))
        if "html" in formats:
            files["html"] = str(directory / f"{basename}.html")
            atomic_write(Path(files["html"]), render_html(report, html_template, output_limit))
        if "csv" in formats:
            files["csv"] = str(directory / f"{basename}.csv")
            atomic_write(Path(files["csv"]), render_csv(report))
        summary_path = request.get("github_step_summary")
        if summary_path and "markdown" in formats:
            if len(markdown.encode("utf-8")) > STEP_SUMMARY_LIMIT:
                markdown = render_markdown(report, template_path, output_limit, compact=True)
            key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:12]
            update_step_summary(Path(summary_path), key, markdown)
            files["github_step_summary"] = str(summary_path)

    return {
        "verdict": report["verdict"],
        "reasons": report["reasons"],
        "summary": report["summary"],
        "hosts": {h["host"]: {"verdict": h["verdict"], "reasons": h["reasons"], "summary": h["summary"]} for h in report["hosts"]},
        "waivers": {
            "expired": sum(w["state"] == "expired" for w in report["waivers"]),
            "unused": sum(w["state"] == "unused" for w in report["waivers"]),
        },
        "files": files,
        "console": console_lines(report, files),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--request", help="the request as a JSON file (default: read it from stdin)")
    args = parser.parse_args(argv)
    try:
        raw = Path(args.request).read_text(encoding="utf-8") if args.request else sys.stdin.read()
        request = json.loads(raw)
        outcome = run(request)
    except RequestError as exc:
        print(f"test_results_report: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"test_results_report: the request is not valid JSON: {exc}", file=sys.stderr)
        return 2
    json.dump(outcome, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
