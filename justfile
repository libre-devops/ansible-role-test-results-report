# Libre DevOps Ansible role libre_devops.test_results_report. Run `just` to list the recipes.
#
# CI runs these same recipes, so a local run shows what CI will do. You need uv
# (https://docs.astral.sh/uv/) and podman. Install just with `uv tool install rust-just`
# or `brew install just`.

set shell := ["bash", "-euo", "pipefail", "-c"]

role := "libre_devops.test_results_report"
github_owner := "libre-devops"
github_repo := "ansible-role-test-results-report"

# List the recipes.
default:
    @just --list

# Install the pinned Python tools and the collections the tests use.
deps:
    uv sync --locked
    uv run ansible-galaxy collection install -r requirements.yml -p .ansible/collections

# Lint the YAML, the role (ansible-lint, production profile), the Python (ruff) and the workflows (zizmor).
lint: deps
    uv run yamllint .
    @uv run --quiet python -c "import sys, yaml; d = yaml.safe_load(open('meta/main.yml'))['galaxy_info']['description']; sys.exit(f'galaxy_info.description is {len(d)} characters: Galaxy refuses more than 255' if len(d) > 255 else 0)"
    uv run ansible-lint
    uv run ruff check files tests
    uv run ruff format --check files tests
    uv run zizmor --offline .github/workflows

# Run the report script's unit tests, which also check every report against the published JSON Schema.
unit: deps
    uv run pytest

# List the Molecule scenarios.
scenarios:
    @find molecule -mindepth 2 -maxdepth 2 -name molecule.yml -printf '%h\n' | xargs -n1 basename | sort

# Run one scenario (every platform at once: the report needs more than one host): just test gate
test scenario="default": deps
    uv run molecule test -s '{{ scenario }}'

# Run every scenario, one at a time.
test-all: deps
    for s in $(just scenarios); do just test "$s"; done

# Run a scenario and leave the containers running, pass or fail: just converge default
converge scenario="default": deps
    uv run molecule test -s '{{ scenario }}' --destroy never

# Remove the containers of a scenario.
destroy scenario="default":
    uv run molecule destroy -s '{{ scenario }}'

# Rebuild a report from a directory of host files with a request of your own: just rebuild request.json
rebuild request:
    uv run python files/test_results_report.py --request '{{ request }}'

# Scan the full git history for secrets (needs gitleaks on PATH).
secrets:
    gitleaks git --redact --verbose .

# Tag a release and push the tag. CI then imports it into Galaxy: just release 1.0.0
release version:
    [[ '{{ version }}' =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be semver, for example 1.0.0" >&2; exit 1; }
    git tag -a '{{ version }}' -m 'Release {{ version }}'
    git push origin '{{ version }}'

# scripts/galaxy-import.py says why this does not use `ansible-galaxy role import`.
# Import a release tag into Galaxy, then install it from Galaxy to check (needs GALAXY_API_KEY): just galaxy-import 1.0.0
galaxy-import tag:
    [[ '{{ tag }}' =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "tag must be semver, for example 1.0.0" >&2; exit 1; }
    uv run --no-project python scripts/galaxy-import.py '{{ role }}' '{{ github_owner }}' '{{ github_repo }}' '{{ tag }}'
    dir="$(mktemp -d)" && trap 'rm -rf "$dir"' EXIT && uv run ansible-galaxy role install --roles-path "$dir" '{{ role }},{{ tag }}'
