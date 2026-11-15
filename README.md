<div align="center">
  <a href="https://libredevops.org">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://libredevops.org/assets/libre-devops-white.png">
      <img alt="Libre DevOps" src="https://libredevops.org/assets/libre-devops-black.png" width="300">
    </picture>
  </a>
</div>

# Ansible Role: Test results report

Collects the results that checks roles leave on each host into one report, judges it against thresholds you set, and writes it on the control node three ways: **JSON** for pipelines to parse, **Markdown** for people to read, and **JUnit XML** for CI test views. It fails the play when the results do not meet the thresholds.

It runs no checks itself. Checks roles such as [libre_devops.mde_linux_checks](https://github.com/libre-devops/ansible-role-mde-linux-checks) do the testing and leave their results in a documented format; this role reads any role's results in that format, so one report covers every product a test touches.

[![CI](https://github.com/libre-devops/ansible-role-test-results-report/actions/workflows/ci.yml/badge.svg)](https://github.com/libre-devops/ansible-role-test-results-report/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/libre-devops/ansible-role-test-results-report?sort=semver&label=release)](https://github.com/libre-devops/ansible-role-test-results-report/releases/latest)
[![Galaxy](https://img.shields.io/badge/galaxy-libre__devops.test__results__report-EE0000?logo=ansible&logoColor=white)](https://galaxy.ansible.com/ui/standalone/roles/libre_devops/test_results_report/documentation/)
[![Licence](https://img.shields.io/github/license/libre-devops/ansible-role-test-results-report)](./LICENSE)

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How a result is judged](#how-a-result-is-judged)
- [Thresholds](#thresholds)
- [Waivers](#waivers)
- [Limits on metrics](#limits-on-metrics)
- [Comparing with an earlier run](#comparing-with-an-earlier-run)
- [The report files](#the-report-files)
- [Reading the report in a pipeline](#reading-the-report-in-a-pipeline)
- [Several runs, one report](#several-runs-one-report)
- [Writing a checks role](#writing-a-checks-role)
- [Role variables](#role-variables)
- [Troubleshooting](#troubleshooting)
- [Developing](#developing)
- [Releasing](#releasing)

## What it does

1. On each host, finds the facts whose names end in `_test_results` (or the ones you name), which is where checks roles leave their results.
2. Writes each host's results, with a few facts about the host (distribution, kernel, size), to `<dir>/hosts/<host>.json` on the control node.
3. Once per play, builds the report from every host file in the directory, applying your thresholds, waivers and limits, and writes `test-results.json`, `test-results.md` and `test-results.xml`. In GitHub Actions it also puts the Markdown in the job summary.
4. Shows a short summary in the Ansible output and sets the `test_results_report_result` fact.
5. Fails the play if the report failed, naming the hosts and the reasons.

Everything happens on the control node. The managed hosts need nothing.

## Requirements

| What | Detail |
|---|---|
| Control node | `ansible-core` 2.16 or later. The report script uses Python's standard library and Jinja2, which ansible-core already depends on. Linux or macOS (it locks the report directory with `fcntl`). |
| Managed nodes | Anything Ansible reaches: the role does not run on them. |
| A checks role | Something that leaves results in [the documented format](#writing-a-checks-role), earlier in the play. |

## Installation

From Galaxy:

```bash
ansible-galaxy role install libre_devops.test_results_report
```

Or pin a release from GitHub, as in [`examples/requirements.yml`](./examples/requirements.yml). Without Galaxy, or to keep a copy inside your organisation, see [Using the role without Galaxy](./docs/using-without-galaxy.md).

## Quick start

Run the checks, then the report, in one play:

```yaml
- name: Test the Defender for Endpoint release
  hosts: release_test
  become: true
  roles:
    - role: libre_devops.mde_linux_checks
    - role: libre_devops.test_results_report
      vars:
        test_results_report_title: mdatp 101.26101.0000 release test
        test_results_report_dir: "{{ playbook_dir }}/artifacts/test-results"
```

The play fails if any check fails. The report is in `artifacts/test-results/`: open `test-results.md` first.

## How a result is judged

A checks role reports each check with a **status** and a **severity**:

| Status | Meaning |
|---|---|
| `passed` | The check ran and the answer was yes. |
| `failed` | The check ran and the answer was no. |
| `error` | The check could not get an answer: a command failed or timed out. |
| `skipped` | The check did not apply here; its detail says why. |

Severities, highest first: `critical`, `high`, `medium`, `low`, `info`.

The report then gives every result an **outcome**, which is what it means for the verdict:

| Outcome | When | Counts against the verdict |
|---|---|---|
| Passed | It passed, and no limit was breached. | No |
| **Failed** | It failed, or breached a limit, at or above `test_results_report_fail_severity`, and no waiver covers it. | **Yes** |
| **Error** | Its status was error, at or above that severity, with no waiver. Also a passed check with a limit on a metric it did not report. | **Yes** |
| Warning | It failed or errored below that severity. | No |
| Waived | It failed or errored, and a waiver accepts it. | No |
| Skipped | It was skipped. | No |

Each host gets a verdict: **failed** if any threshold below is not met, or if it sent no results at all (it was unreachable, or failed before the checks). The report fails when more hosts fail than `test_results_report_max_failed_hosts` allows, or when no host reported.

## Thresholds

Every threshold applies to each host on its own.

| Variable | Default | Fails the host when |
|---|---|---|
| `test_results_report_fail_severity` | `low` | Sets which failures count: failures below it are warnings. |
| `test_results_report_max_failures` | `0` | More failures and errors that count than this. |
| `test_results_report_min_pass_rate` | `0` (off) | The share of checks that ran (not skipped) whose outcome is passed or waived is below this percentage. |
| `test_results_report_min_checks` | `1` | Fewer checks than this ran. A host where everything was skipped proved nothing. |
| `test_results_report_expected_checks` | `[]` | A check listed here did not report, or was skipped. Use it for checks that must never quietly drop out, such as the ones a release decision rests on. |

And one for the whole report:

| Variable | Default | Fails the report when |
|---|---|---|
| `test_results_report_max_failed_hosts` | `0` | More hosts failed than this. |

Three common set-ups:

```yaml
# A release test: everything must pass, and the checks that matter must have run.
test_results_report_fail_severity: low
test_results_report_expected_checks:
  - mde_linux_checks/real_time_protection
  - mde_linux_checks/eicar_detection
  - mde_linux_checks/service_survives_reboot

# A health audit of an estate: only high and critical failures count, and up to two of a
# hundred servers may fail before the run does (they are in the report either way).
test_results_report_fail_severity: high
test_results_report_max_failed_hosts: 2

# A large compliance suite: allow some failures as long as 95% pass.
test_results_report_max_failures: 50
test_results_report_min_pass_rate: 95
```

## Waivers

A waiver accepts a known failure, with a reason, so it does not fail the run but stays visible: waived results have their own section in the Markdown report.

```yaml
test_results_report_waivers:
  - check: eicar_detection
    reason: The lab tenant runs the antivirus in passive mode
    reference: CHG0012345
    expires: 2026-12-31
  - check: upgrade_*                 # wildcards
    suite: mde_linux_checks
    hosts: [rhel8]                   # inventory names or labels, wildcards allowed
    reason: RHEL 8 images lack the eBPF feature these checks rely on
```

| Key | Required | Meaning |
|---|---|---|
| `check` | Yes | The check id. Wildcards (`*`, `?`, `[...]`) work. |
| `suite` | No | The suite id (the checks role). Default: any. |
| `hosts` | No | Host names or labels it applies to. Default: all. |
| `reason` | Yes | Why the failure is accepted. It is shown wherever the result is. |
| `reference` | No | A ticket or change number. |
| `expires` | No | The last day it applies (`YYYY-MM-DD`). After that the failure counts again and the report marks the waiver **Expired**, so waivers cannot be forgotten. |

A waiver never hides a skipped check, and the report lists waivers that matched nothing as **Unused**, so stale ones can be removed.

## Limits on metrics

Checks can report numbers (scan durations, memory, CPU). A limit turns a number into a pass or fail:

```yaml
test_results_report_limits:
  - check: quick_scan
    metric: duration_seconds
    max: 900
  - check: resource_usage
    metric: memory_mib
    max: 1024
    max_increase_percent: 25         # against test_results_report_compare_to
    severity: medium                 # a breach counts as a medium failure
```

| Key | Meaning |
|---|---|
| `check`, `suite`, `hosts` | Which results it applies to, as for waivers. |
| `metric` | The metric's name, as the check reports it. |
| `min`, `max` | Absolute bounds. |
| `max_increase_percent`, `max_decrease_percent` | Relative to the same check on the same host label in the report you compare with. Without one, or when that report lacks the metric, only the absolute bounds apply. |
| `severity` | What a breach counts as, when the limit alone fails the check. Without it the check's own severity applies. When the check failed anyway, a limit can only raise its severity. |

A limit fails closed. If a check that ran does not report the metric a limit names, perhaps because a newer checks role renamed it, the check becomes an **error**, rather than the limit quietly no longer applying.

## Comparing with an earlier run

Point `test_results_report_compare_to` at an earlier run's `test-results.json`, such as last month's release test. The report then says, for each check, whether it is **new**, **fixed**, **regressed** or unchanged. It also lists checks that the earlier run had but this one does not, which is how a check that silently stopped running gets noticed. The percentage limits use the earlier run's metrics as their baseline.

Results are matched on the host's **label**, the suite and the check id. Release tests build new VMs each time, so give each host a stable label:

```yaml
# group_vars/rhel9.yml
test_results_report_host_label: rhel9
```

## The report files

All three files are written to `test_results_report_dir`, with `test_results_report_basename` as the name:

| File | For | What is in it |
|---|---|---|
| `test-results.md` | People | The verdict and one-line summary first, then why it failed, then a table of hosts. After that come the checks that need attention, ordered by severity, with what was expected, what was seen, what to do and the evidence. Then the waived failures, any changes since the earlier run, every result per host with the suite's properties (the product version, for example), and how the verdict was reached. It is shown in the GitHub job summary too. |
| `test-results.json` | Pipelines | Everything, in three flat tables (`hosts`, `suites`, `results`) plus the `summary`, the `policy` applied, the `waivers` and `limits` with how often each matched, and the `comparison`. Every key is always present, with `null` for no value. The format is versioned (`schema: libre_devops.test_results_report/v1`) and published as a JSON Schema: [`files/schemas/test-results-report.schema.json`](./files/schemas/test-results-report.schema.json). |
| `test-results.xml` | CI test views | JUnit XML: one `<testsuite>` per host and suite, one `<testcase>` per check, named `<id>: <name>` with classname `<host>.<suite>`. Only failures that count are `<failure>` or `<error>`. Waived, warning and skipped results are `<skipped>` with a message saying which, so a CI tool that fails on test failures agrees with the verdict. A host that sent nothing is an `<error>`. |
| `hosts/<host>.json` | Rebuilding | Each host's results as the checks roles reported them. |

Every result in the JSON carries both what the checks role reported (`reported_status`, `reported_severity`) and what the report made of it (`status` and `severity` after limits, then `outcome`), so a pipeline can apply a policy of its own to the raw results.

## Reading the report in a pipeline

The JSON is meant to be queried. Some `jq` examples:

```bash
r=test-results/test-results.json

# The verdict, as an exit code.
jq -e '.verdict == "passed"' "$r"

# Every result that counts against the verdict.
jq -r '.results[] | select(.outcome == "failed" or .outcome == "error")
       | "\(.host)\t\(.severity)\t\(.suite)/\(.id)\t\(.detail)"' "$r"

# A gate of your own: no critical or high failures, waivers or not.
jq -e '[.results[] | select(.reported_status != "passed" and .reported_status != "skipped"
                           and (.reported_severity == "critical" or .reported_severity == "high"))] | length == 0' "$r"

# One metric across hosts.
jq -r '.results[] | select(.id == "quick_scan") | "\(.label)\t\(.metrics.duration_seconds)"' "$r"

# What regressed since the earlier run.
jq -r '.results[] | select(.change == "regressed") | "\(.label) \(.id)"' "$r"
```

PowerShell reads it as well:

```powershell
$report = Get-Content test-results/test-results.json -Raw | ConvertFrom-Json
$report.results | Where-Object outcome -in 'failed', 'error' | Format-Table host, severity, id, detail
```

Publishing the files in CI:

```yaml
# GitHub Actions: the Markdown is in the job summary already; keep the files, and show the JUnit.
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: test-results
    path: test-results/

# Azure Pipelines: test results tab, plus the Markdown as a summary.
- task: PublishTestResults@2
  condition: always()
  inputs:
    testResultsFormat: JUnit
    testResultsFiles: test-results/test-results.xml
- script: echo "##vso[task.uploadsummary]$(System.DefaultWorkingDirectory)/test-results/test-results.md"
  condition: always()
```

## Several runs, one report

Each run writes its hosts' files to `hosts/` and then rebuilds the report from every host file in the directory, under a lock. So runs that share a directory add up to one report.

This matters with [terraform-ansible-role](https://github.com/libre-devops/terraform-ansible-role), which runs one `ansible-playbook` per host, in parallel. Each run rebuilds the report with the hosts that have finished so far, so the last one to finish leaves the complete report. Two consequences:

- **Give each run its own directory**, such as one named after the run or the plan's timestamp, or the report includes hosts from an earlier run.
- **Each run's verdict covers the hosts reported so far.** A host that failed fails the run that reported it, or a later one. With `test_results_report_max_failed_hosts` above 0, the run that reports one failure too many is the one that fails.

`serial` plays work the same way: each batch rebuilds the report, and a later batch's hosts do not count as missing until their batch has run.

## Writing a checks role

A checks role sets one fact per host, named `<role>_test_results`, in this format. It is published as a JSON Schema: [`files/schemas/test-results.schema.json`](./files/schemas/test-results.schema.json).

```yaml
- name: Publish the results
  ansible.builtin.set_fact:
    my_product_checks_test_results:
      schema: libre_devops.test_results/v1
      suite: my_product_checks              # a stable id: letters, digits, _ . -
      name: My product
      started: "2026-11-15T10:00:00Z"
      finished: "2026-11-15T10:04:12Z"
      properties:                           # what was tested: shown with the suite
        product_version: 4.2.1
      checks:
        - id: service_running               # stable: waivers, limits and comparisons use it
          name: The service is running      # a statement that is true when it passes
          status: passed                    # passed, failed, error or skipped
          severity: critical                # critical, high, medium, low or info
          category: stability
          detail: myproductd is active      # one line: what was found
        - id: scan_time
          name: A full scan completes
          status: passed
          severity: medium
          metrics:
            duration_seconds: 412.6         # unit in the name: duration_seconds, memory_mib
        - id: cloud_reachable
          name: The cloud service is reachable
          status: skipped
          detail: The host has no route to the internet
```

Each check can also have `expected`, `actual`, `remediation`, `docs` (a link) and `output` (evidence such as command output: the JSON keeps 64 KiB, the Markdown `test_results_report_output_limit` characters), and `duration_seconds`.

Rules that keep reports useful:

- **Never reuse an id** for a different check, and never rename one without saying so in your changelog: waivers, limits, expected checks and comparisons all select by id.
- **Skip with a reason** rather than leaving a check out, so a reader knows it was considered. Record `error` when the check could not get an answer, not `failed`.
- **Do not fail the play** from a checks role. Record the result and carry on, so the report shows every result, and let this role decide.
- **Report numbers as metrics**, not only in the detail, so they can be limited and compared.

A suite that breaks the format is not dropped. It shows up as one critical **error** result saying what is wrong, so a broken checks role fails the report rather than vanishing from it.

## Role variables

Every variable is validated against [`meta/argument_specs.yml`](./meta/argument_specs.yml) when the role starts. Defaults are in [`defaults/main.yml`](./defaults/main.yml).

| Variable | Default | Description |
|---|---|---|
| `test_results_report_dir` | `{{ playbook_dir }}/test-results` | Directory on the control node for the report. One per run: hosts an earlier run left in it are reported too. A relative path is under the playbook's directory. |
| `test_results_report_basename` | `test-results` | The files' name, without an extension. |
| `test_results_report_formats` | `[json, markdown, junit]` | Which files to write. |
| `test_results_report_title` | `Test results` | The report's title. |
| `test_results_report_properties` | `{}` | What to record with the report: the version tested, the pipeline run, the commit. |
| `test_results_report_facts` | `[]` | The facts that hold each host's results. Empty finds every fact ending in `_test_results`. |
| `test_results_report_host_label` | `{{ inventory_hostname }}` | A stable name for the host, which comparisons match on. |
| `test_results_report_fail_severity` | `low` | The lowest severity whose failures count. See [Thresholds](#thresholds). |
| `test_results_report_max_failures` | `0` | Failures allowed per host. |
| `test_results_report_min_pass_rate` | `0` | Lowest pass rate per host, in percent. `0` turns it off. |
| `test_results_report_min_checks` | `1` | Fewest checks that must run per host. |
| `test_results_report_expected_checks` | `[]` | Checks that must run on every host they select. |
| `test_results_report_max_failed_hosts` | `0` | Hosts allowed to fail before the report does. |
| `test_results_report_waivers` | `[]` | Accepted failures. See [Waivers](#waivers). |
| `test_results_report_limits` | `[]` | Limits on metrics. See [Limits on metrics](#limits-on-metrics). |
| `test_results_report_compare_to` | `""` | An earlier `test-results.json` to compare with. |
| `test_results_report_enforce` | `true` | Fail the play when the report fails. |
| `test_results_report_github_step_summary` | `true` | In GitHub Actions, put the Markdown in the job summary. Several runs in one step replace each other's copy rather than adding to it. |
| `test_results_report_markdown_template` | `""` | Your own Jinja2 template for the Markdown. The role's, [`templates/test-results.md.j2`](./templates/test-results.md.j2), is the place to start. |
| `test_results_report_output_limit` | `4000` | The most characters of a check's output the Markdown shows. |

| Fact | What it holds |
|---|---|
| `test_results_report_result` | `verdict`, `reasons`, `summary`, `hosts` (each host's verdict, reasons and counts), `waivers` (how many expired or unused) and `files` (the paths written). |

## Troubleshooting

| Symptom | What to check |
|---|---|
| A host shows **No results** | It was unreachable or failed before the report ran, or no checks role ran on it. The play's output shows which. |
| `0 checks ran (at least 1 expected)` | Every check on the host was skipped. Their details say why. |
| `<suite> reported results this report can read` | The checks role broke the format. The result's detail says how. |
| `test_results_report_facts names facts this host does not have` | The checks role did not run on that host, or its fact has another name. |
| The report includes hosts from another run | The directory was reused. Give each run its own. |
| `policy has unknown settings` or `needs a reason` | A threshold, waiver or limit is malformed. The message names it. |

To rebuild a report from the host files with other thresholds, write a request such as `{"dir": "/abs/path/test-results", "policy": {"fail_severity": "high"}}` and run `just rebuild request.json` (or `python3 files/test_results_report.py --request request.json`).

## Developing

You need [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just) and [podman](https://podman.io/). CI runs the same recipes.

| Recipe | What it does |
|---|---|
| `just deps` | Installs the pinned Python tools (`uv.lock`) and the test collections into `.ansible/`. |
| `just lint` | `yamllint`, `ansible-lint` on the production profile, `ruff` and `zizmor`. |
| `just unit` | The report script's unit tests, with every report checked against the published JSON Schema. |
| `just test [scenario]` | One Molecule scenario on both test hosts: `default` (a passing report with waivers, limits and warnings), `gate` (a failing report stops the play, or does not with `enforce: false`) or `serial` (batches add up to one report). |
| `just converge [scenario]` | Runs a scenario and leaves the containers, pass or fail. `just destroy` removes them. |
| `just secrets` | Scans the git history with gitleaks. |

## Releasing

1. Move the Unreleased entries in [`CHANGELOG.md`](./CHANGELOG.md) under the new version.
2. Run `just release 1.2.3`. It tags the commit and pushes the tag.
3. The Release workflow creates the GitHub release, imports the tag into Galaxy and installs it back from Galaxy to check. It needs the `GALAXY_API_KEY` secret from the libre-devops organisation. The import uses Galaxy's API through [`scripts/galaxy-import.py`](./scripts/galaxy-import.py) because Galaxy namespaces cannot contain a hyphen (the role namespace is `libre_devops`, the organisation `libre-devops`). If it fails, fix the cause and run the Release workflow by hand with the tag.

## Licence

[MIT](./LICENSE).
