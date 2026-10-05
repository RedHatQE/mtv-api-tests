# Development Workflow

This repository does not revolve around a single `make` target. The practical local workflow is a short loop built around `uv`, `pre-commit`, `tox`, and the checked-in
`Dockerfile`.

For most day-to-day work, the fastest feedback loop is:

```bash
uv sync
pre-commit run --all-files
tox -e pytest-check
tox -e unused-code
podman build -f Dockerfile -t mtv-api-tests .
```

## Prerequisites

The project targets Python `>=3.12, <3.14`, and it keeps a small `dev` dependency group for interactive tooling.

```toml
[project]
requires-python = ">=3.12, <3.14"

[dependency-groups]
dev = ["ipdb>=0.13.13", "ipython>=8.12.3", "python-jenkins>=1.8.2"]
```

> **Note:** `pre-commit` and `tox` are configured in this repository, but they are not declared as project dependencies in `pyproject.toml`. If those commands are not already
> available on your machine, install them with the Python CLI tool manager you normally use.

## Sync Dependencies

Run `uv sync` from the repository root to create or refresh the local environment from `uv.lock`. That is the right starting point after cloning the repo or switching to a branch
with dependency changes.

When you want the strictest possible sync, use `uv sync --locked`. That is the exact mode used by the container build, which also layers unreleased upstream commits on top:

```dockerfile
RUN uv sync --locked \
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-wrapper.git@${OPENSHIFT_PYTHON_WRAPPER_COMMIT}"; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-utilities.git@${OPENSHIFT_PYTHON_UTILITIES_COMMIT}"; fi
```

> **Tip:** If you want to reproduce the container's dependency resolution locally, run `uv sync --locked` before troubleshooting.

## Pre-commit Checks

`pre-commit run --all-files` is the main local quality gate. It brings together repository hygiene checks, secret scanning, Python linting and formatting, typing, and Markdown
linting.

Key hooks from `.pre-commit-config.yaml`:

```yaml
default_language_version:
  python: python3.13

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: check-added-large-files
      - id: check-docstring-first
      - id: check-merge-conflict
      - id: check-symlinks
      - id: detect-private-key
      - id: mixed-line-ending
      - id: trailing-whitespace
        args: [--markdown-linebreak-ext=md]
      - id: end-of-file-fixer
      - id: check-ast
      - id: check-toml

  - repo: https://github.com/PyCQA/flake8
    rev: 7.4.1
    hooks:
      - id: flake8
        args: [--config=.flake8]
        additional_dependencies: [flake8-mutable]

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.9
    hooks:
      - id: ruff
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v2.3.1
    hooks:
      - id: mypy
        additional_dependencies:
          [
            "types-pyvmomi",
            "types-requests",
            "types-six",
            "types-pytz",
            "types-PyYAML",
            "types-paramiko",
          ]

  - repo: https://github.com/DavidAnson/markdownlint-cli2
    rev: v0.23.3
    hooks:
      - id: markdownlint-cli2
        args: ["--fix"]
        exclude: ^docs/
```

The full hook set also includes `check-executables-have-shebangs`, `debug-statements`, `check-builtin-literals`, `detect-secrets`, and `gitleaks`.

> **Warning:** `markdownlint-cli2`, `detect-secrets`, and `gitleaks` all carry `exclude: ^docs/`. Changes under `docs/` are not covered by the pre-commit quality gate at all. If
> you edit a Markdown page, lint it yourself or run the site generator, which reports structural problems.

Use the full suite when you want the closest thing to a local CI gate:

```bash
pre-commit run --all-files
```

Or rerun a single hook while iterating on one kind of issue:

```bash
pre-commit run ruff --all-files
pre-commit run ruff-format --all-files
pre-commit run mypy --all-files
pre-commit run flake8 --all-files
pre-commit run check-no-except-exception --all-files
```

> **Warning:** The hook environments default to `python3.13`. If that interpreter is missing on your workstation, `pre-commit` can fail while creating hook environments even
> though the project itself supports Python 3.12.

## AGENTS Rules Enforced By Local Hooks

Eight hooks under `repo: local` in `.pre-commit-config.yaml` run AST-based checks from `scripts/hooks/`. They exist because several `AGENTS.md` rules are easy to state and easy to
violate silently.

| Hook id | Bans or requires |
| --- | --- |
| `check-no-kubernetes-runtime` | Runtime `kubernetes.*` imports outside `TYPE_CHECKING`, except `kubernetes.dynamic.exceptions` and `from kubernetes.dynamic import exceptions` |
| `check-no-dynamicclient-construct` | Constructing `DynamicClient()` directly; use `get_client()` instead |
| `check-no-except-exception` | `except Exception`, including tuple and list forms such as `except (Exception, OSError)`, outside pytest hooks in `conftest.py` |
| `check-no-runtimeerror` | `raise RuntimeError`, including bare-name raises, outside pytest hooks in `conftest.py` |
| `check-no-module-load-source-providers` | Module-level `load_source_providers()` calls under `tests/`, which silently ignore `--providers-json` |
| `check-exceptions-location` | `Exception` subclasses defined anywhere other than `exceptions/exceptions.py` |
| `check-test-file-location` | `tests/test_*.py` directly under `tests/`; test modules must live in a feature subdirectory |
| `check-autouse-fixtures` | `autouse=True` on any fixture other than `autouse_fixtures` in `conftest.py` |

`scripts/hooks/README.md` documents how to run a single check directly:

```bash
pre-commit run check-no-except-exception --all-files
.venv/bin/python scripts/hooks/check_no_except_exception.py
```

### Baseline ratchet

Four hooks still fail on legacy code. Those findings are grandfathered in `scripts/hooks/baselines/<hook_id>.txt`, so CI stays green while new violations still fail:

- `check_exceptions_location.txt`
- `check_no_except_exception.txt`
- `check_no_kubernetes_runtime.txt`
- `check_no_runtimeerror.txt`

The filename uses the snake_case script stem, not the kebab-case pre-commit id. Each line is `relative/posix/path:<16-char-sha256-hex>`, where the fingerprint is
`sha256(span)[:16]` over the exact source span. Matching is on path plus content fingerprint with occurrence counts, not line numbers, so refactors that move a violation do not
resurrect it, but editing any line inside a baselined span does.

> **Tip:** When you fix a baselined violation, delete its baseline line. Do not add baseline lines casually; only do it as a deliberate grandfathering decision.

## Linting, Typing, and Docs Rules

The Python tool settings live in `pyproject.toml`:

```toml
[tool.ruff]
preview = true
line-length = 120
fix = true
output-format = "grouped"

[tool.ruff.format]
exclude = [".git", ".venv", ".mypy_cache", ".tox", "__pycache__"]

[tool.ruff.lint]
select = ["PLC0415"]

[tool.mypy]
disallow_any_generics = false
disallow_incomplete_defs = true
no_implicit_optional = true
show_error_codes = true
warn_unused_ignores = true

[[tool.mypy.overrides]]
module = "paramiko"
ignore_missing_imports = true
```

In practice, that means:

- Ruff is configured to auto-fix where possible, so it is usually the first thing to rerun after Python edits.
- Mypy is part of the default quality gate, including third-party type stubs for common dependencies used in this repository.
- Flake8 is intentionally narrow here. It is focused on the `flake8-mutable` rule rather than acting as a second full Python linter.

```ini
[flake8]
select=M511

exclude =
    doc,
    .tox,
    .git,
    .yml,
    Pipfile.*,
    docs/*,
    .cache/*
```

If you are editing documentation, `markdownlint-cli2` is part of the same workflow but is configured with `exclude: ^docs/`, so it does not touch pages in this site. Its
repo-level config, in `.markdownlint.yaml`, allows fairly wide lines and a few inline HTML elements:

```yaml
MD013:
  line_length: 180

MD033:
  allowed_elements:
    - details
    - summary
    - strong
```

## Rebuilding The Documentation Site

`docs/` is a [pi-docsite](https://pypi.org/project/pi-docsite/) project. Per `AGENTS.md`, edit only the Markdown sources and never the generated output:

| Path | Role |
| --- | --- |
| `docs/*.md` | Sources, the only files humans edit |
| `docs/nav.json` | Sidebar layout, groups and page order |
| `docs/*.html`, `docs/assets/`, `docs/llms.txt`, `docs/llms-full.txt` | Generated, never hand-edited |

Rebuild after any change to a `docs/*.md` file or to `docs/nav.json`:

```bash
uvx pi-docsite --docs-dir docs \
  --tagline "Pytest-based integration suite for validating Migration Toolkit for Virtualization migrations into OpenShift Virtualization."
```

The generator enforces the invariants this site depends on: each page has exactly one H1, every page appears in `nav.json`, and a `nav.json` slug without a `.md` behind it is a
build error. Output is byte-for-byte reproducible, so churn in untouched files means the generator changed rather than the build being stale.

> **Tip:** Because `markdownlint-cli2` excludes `docs/`, the generator is your structural check on documentation changes. Run it before committing.

> **Note:** Search on the site only works when `docs/` is served over HTTP. Browsers block `fetch()` of `search-index.json` over `file://`.

## Tox Targets

Unlike some Python projects, tox is not the main entry point for every local check here. This repository defines two focused tox environments in `tox.toml`:

```toml
skipsdist = true
env_list = ["pytest-check", "unused-code"]

[env.pytest-check]
commands = [
  ["uv", "run", "pytest", "--setup-plan"],
  ["uv", "run", "pytest", "--collect-only"],
]
description = "Run pytest collect-only and setup-plan"
deps = ["uv"]

[env.unused-code]
description = "Find unused code"
deps = ["python-utility-scripts"]
commands = [["pyutils-unusedcode", "--exclude-function-prefixes", "pytest_"]]
```

Run them with:

```bash
tox -e pytest-check
tox -e unused-code
```

These environments do two different jobs:

- `pytest-check` validates pytest structure without executing real migrations.
- `unused-code` runs `pyutils-unusedcode` and ignores pytest hook-style function names with `--exclude-function-prefixes pytest_`.

`pytest-check` is intentionally safe because the repository treats both `--setup-plan` and `--collect-only` as dry-run modes:

```python
def is_dry_run(config: pytest.Config) -> bool:
    """Check if pytest was invoked in dry-run mode (collectonly or setupplan)."""
```

When `is_dry_run()` is true, `conftest.py` skips the required-config check, skips session finish, and skips the per-test data collector.

> **Warning:** `AGENTS.md` prohibits running `pytest` or `uv run pytest` directly. Every test here needs a live OpenShift cluster, provider connections, and real credentials.
> Stick to `--collect-only`, `--setup-plan`, and `tox -e pytest-check`.

> **Tip:** Run `tox -e pytest-check` after changing fixtures, parametrization, markers, or imports. It is a fast way to catch collection breakage before you try a real
> environment-backed run.

## Real Pytest Runs

A plain `pytest` invocation already picks up repository defaults from `pytest.ini`. Test discovery is limited to `tests/`, and the default options load
`tests/tests_config/config.py`, write JUnit XML, enforce strict markers, and enable `loadscope` distribution for `pytest-xdist`.

```ini
[pytest]
testpaths = tests

addopts =
  -s
  -o log_cli=true
  -p no:logging
  --tc-file=tests/tests_config/config.py
  --tc-format=python
  --junit-xml=junit-report.xml
  --show-progress
  --strict-markers
  --dist=loadscope

markers =
    tier0: Core functionality tests (smoke tests)
    tier1: Extended functionality tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    shared_disk: Shared disk migration tests (vSphere only)
    incremental: marks tests as incremental (xfail on previous failure)
    ...
```

Real test execution is not a unit-test-only workflow. In `conftest.py`, the session requires both `storage_class` and `source_provider` unless pytest is running in dry-run mode:

```python
required_config = ("storage_class", "source_provider")

if not is_dry_run(session.config):
    BASIC_LOGGER.info(f"{separator(symbol_='-', val='SESSION START')}")

    missing_configs: list[str] = []

    for _req in required_config:
        if not py_config.get(_req):
            missing_configs.append(_req)

    if missing_configs:
        pytest.exit(reason=f"Some required config is missing {required_config=} - {missing_configs=}", returncode=1)
```

An actual checked-in example of a real test command appears in the copy-offload documentation:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

> **Warning:** Use real `pytest` runs only when you have access to a live OpenShift/MTV environment and valid provider configuration. For routine local verification, `pre-commit`
> and `tox -e pytest-check` are the safer defaults.

> **Tip:** The repo includes `.providers.json.example` and ignores `.providers.json` in `.gitignore`. Use the example as a reference, but your real `.providers.json` must be plain
> JSON: `load_source_providers()` in `utilities/utils.py` calls `json.loads()` directly, and the example file carries inline `# pragma: allowlist secret` comments for
> documentation that a strict JSON parser rejects. Field names and required/optional status are defined in `providers_schema.json`.

## Container Builds

Use the checked-in `Dockerfile` when you want a clean, reproducible runtime that matches the repository's containerized execution path. It is a two-stage build on Red Hat UBI 10
minimal: a builder stage installs compilers, copies the source, and runs `uv sync --locked`; a runtime stage copies only the resulting environment and drops the build toolchain.

The relevant parts:

```dockerfile
FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb... AS builder

ARG APP_DIR=/app
ENV UV_PYTHON=python3.12

COPY cli cli
COPY docs docs
COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini ./

RUN uv sync --locked \
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-wrapper.git@${OPENSHIFT_PYTHON_WRAPPER_COMMIT}"; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 -r rm -rfv \
  && rm -rf ${APP_DIR}/.cache

FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb...

USER 1001

CMD ["uv", "run", "pytest", "--collect-only"]
```

The runtime stage also sets `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1` so nothing is lost from the OpenShift job log, and points `JUNITFILE` at `${APP_DIR}/output/`.

Build locally with either Podman or Docker:

```bash
podman build -f Dockerfile -t mtv-api-tests .
docker build -f Dockerfile -t mtv-api-tests .
```

If you need to validate unreleased dependency changes in `openshift-python-wrapper` or `openshift-python-utilities`, the `Dockerfile` already exposes build arguments for that:

```bash
podman build \
  -f Dockerfile \
  -t mtv-api-tests \
  --build-arg OPENSHIFT_PYTHON_WRAPPER_COMMIT=<commit> \
  --build-arg OPENSHIFT_PYTHON_UTILITIES_COMMIT=<commit> \
  .
```

Because the default container command is `uv run pytest --collect-only`, you can smoke-test the image without starting a real migration run:

```bash
podman run --rm mtv-api-tests
```

That makes the container build a good final check when you touch packaging, dependency resolution, or anything that could behave differently outside your local shell.
