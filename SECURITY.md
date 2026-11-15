# Security policy

## Reporting a vulnerability

Report vulnerabilities in this role privately, through [GitHub's private vulnerability reporting](https://github.com/libre-devops/ansible-role-test-results-report/security/advisories/new). Do not open a public issue.

## What to know

- The role runs only on the control node. It reads the facts checks roles set and writes files under `test_results_report_dir`; it never runs anything from the results.
- The report holds whatever the checks roles put in their results, command output included. Treat the report directory like the rest of the run's logs: publish it only where the hosts' details may go.
- The Markdown report escapes every value from the results, so a result cannot inject HTML or break the report's tables, and the JUnit report strips the control characters XML cannot carry.
- In GitHub Actions the Markdown report goes into the job summary (`test_results_report_github_step_summary`), which anyone who can read the run can see. Turn it off for repositories whose runs are public if the results hold anything sensitive.
