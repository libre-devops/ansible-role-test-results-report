# Contributing

Issues and pull requests are welcome. Open an issue first for anything large, so we can agree the approach before you write it.

## Set up

You need [uv](https://docs.astral.sh/uv/), [just](https://github.com/casey/just) and [podman](https://podman.io/). Then:

```bash
just deps      # pinned Python tools and the test collections
just lint      # yamllint, ansible-lint (production profile), ruff, zizmor
just unit      # the report script's unit tests
just test default
```

`just` on its own lists every recipe.

## Rules

- Every variable is in `defaults/main.yml`, `meta/argument_specs.yml` and the README variables table, with the same default and description in all three.
- Internal variables start with `__test_results_report_` and live in `vars/main.yml` or are registered in tasks.
- The report logic lives in `files/test_results_report.py`, which uses only the standard library and Jinja2 (ansible-core's own dependency), runs under `ansible_playbook_python`, and has unit tests for every behaviour.
- The two published formats, the results a checks role leaves (`files/schemas/test-results.schema.json`) and the JSON report (`files/schemas/test-results-report.schema.json`), are contracts. A change that removes or renames a field, or changes what one means, needs a new schema version. Adding a field needs the schema updated in the same change; the unit tests validate every report against it.
- Use fully qualified module names, name every task, and keep `ansible-lint` on the production profile clean. A `noqa` needs a comment saying why.
- Docs and comments are in British English.

## Commits and releases

Write commit subjects in the imperative, wrapped at 72 characters. Add an entry under Unreleased in `CHANGELOG.md`.

Maintainers release with `just release 1.2.3`, which tags and pushes. The Release workflow then creates the GitHub release and imports the tag into Galaxy.
