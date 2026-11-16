# Changelog

Notable changes to this role. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

## 1.0.1 - 2026-11-16

### Added

- Diagrams: Mermaid flowcharts in the README (how the report is built, and how a result is judged), and `docs/diagrams/test-results-report.drawio` with the same two pages in more detail.

## 1.0.0 - 2026-11-16

### Added

- First release.
- Reads the results checks roles leave on each host (facts ending in `_test_results`, format `libre_devops.test_results/v1`) and writes one report on the control node as JSON (`libre_devops.test_results_report/v1`), Markdown and JUnit XML, with both formats published as JSON Schemas.
- Thresholds per host (`fail_severity`, `max_failures`, `min_pass_rate`, `min_checks`, `expected_checks`) and for the run (`max_failed_hosts`), waivers with reasons, references and expiry dates, and limits on metrics, absolute or relative to an earlier report.
- Comparison with an earlier report by host label: regressions, fixes, new checks and checks no longer reported.
- An HTML report for people, on by default: one self-contained page in the style of `ldo-cli -o html`, light and dark, with filter, sort and copy as CSV on every table, nothing fetched and a content security policy that runs only its own style and script. A CSV report, one row per result, on request.
- GitLab CI support: skip and waiver reasons as JUnit element text (which GitLab's test reports read), a `dotenv` format (the verdict and counts for later jobs), a `metrics` format (`name value` lines for GitLab's metrics report, and Prometheus), and links on the job's page to the report (`artifacts:reports:annotations`), written automatically in GitLab CI.
- Host files and a locked rebuild, so parallel per-host runs (terraform-ansible-role) and `serial` batches add up to one report; the GitHub job summary is replaced, not appended, on each rebuild.
- A suite that breaks the format shows as a critical error instead of vanishing; a limit on a metric a check did not report is an error instead of a silent pass.
- Unit tests for the report script and Molecule scenarios `default`, `gate` and `serial`.
- `docs/using-without-galaxy.md`.
