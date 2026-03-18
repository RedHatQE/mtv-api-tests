# Development Workflow

This repository does not revolve around a single `make` target. The practical local workflow is a short loop built around `uv`, `pre-commit`, `tox`, and the checked-in `Dockerfile`.

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

> **Note:** `pre-commit` and `tox` are configured in this repository, but they are not declared as project dependencies in `pyproject.toml`. If those commands are not already available on your machine, install them with the Python CLI tool manager you normally use.

## Sync Dependencies

Run `uv sync` from the repository root to create or refresh the local environment from `uv.lock`. That is the right starting point after cloning the repo or switching to a branch with dependency changes.

When you want the strictest possible sync, use `uv sync --locked`. That is the exact mode used by the container build:

```dockerfile
RUN uv sync --locked\
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-wrapper.git@$OPENSHIFT_PYTHON_WRAPPER_COMMIT; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-utilities.git@$OPENSHIFT_PYTHON_UTILITIES_COMMIT; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv \
  && rm -rf ${APP_DIR}/.cache
```

> **Tip:** If you want to reproduce the container's dependency resolution locally, run `uv sync --locked` before troubleshooting.

## Pre-commit Checks

`pre-commit run --all-files` is the main local quality gate. It brings together repository hygiene checks, secret scanning, Python linting and formatting, typing, and Markdown linting.

Key hooks from `.pre-commit-config.yaml`:

```yaml
default_language_version:
  python: python3.13

repos:
  - repo: https://github.com/PyCQA/flake8
    rev: 7.3.0
    hooks:
      - id: flake8
        args: [--config=.flake8]
        additional_dependencies: [flake8-mutable]

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.4
    hooks:
      - id: ruff
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.19.1
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
    rev: v0.21.0
    hooks:
      - id: markdownlint-cli2
        args: ["--fix"]
```

The full hook set also includes `check-added-large-files`, `detect-private-key`, `detect-secrets`, `gitleaks`, `mixed-line-ending`, `trailing-whitespace`, and other small safety checks from `pre-commit-hooks`.

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
pre-commit run markdownlint-cli2 --all-files
```

> **Warning:** The hook environments default to `python3.13`. If that interpreter is missing on your workstation, `pre-commit` can fail while creating hook environments even though the project itself supports Python 3.12.

> **Note:** Secret scanning is part of the default local workflow. If you add examples that look like credentials, expect `detect-secrets` and `gitleaks` to review them.

## Linting, Typing, and Docs Rules

The Python tool settings live in `pyproject.toml`:

```toml
[tool.ruff]
preview = true
line-length = 120
fix = true
output-format = "grouped"

[tool.ruff.lint]
select = ["PLC0415"]

[tool.mypy]
disallow_incomplete_defs = true
no_implicit_optional = true
show_error_codes = true
warn_unused_ignores = true
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

If you are editing documentation, `markdownlint-cli2` is already part of the same workflow. Its repo-level config allows fairly wide lines and a few inline HTML elements often used in docs:

```yaml
MD013:
  line_length: 180

MD033:
  allowed_elements:
    - details
    - summary
    - strong
```

> **Tip:** For docs-only changes, `pre-commit run markdownlint-cli2 --all-files` is usually the fastest targeted check.

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
    return config.option.setupplan or config.option.collectonly
```

> **Tip:** Run `tox -e pytest-check` after changing fixtures, parametrization, markers, or imports. It is a fast way to catch collection breakage before you try a real environment-backed run.

## Real Pytest Runs

A plain `pytest` invocation already picks up repository defaults from `pytest.ini`. Test discovery is limited to `tests/`, and the default options load `tests/tests_config/config.py`, write JUnit XML, enforce strict markers, and enable `loadscope` distribution.

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
  --basetemp=/tmp/pytest
  --show-progress
  --strict-markers
  --jira
  --dist=loadscope
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

> **Warning:** Use real `pytest` runs only when you have access to a live OpenShift/MTV environment and valid provider configuration. For routine local verification, `pre-commit` and `tox -e pytest-check` are the safer defaults.

> **Tip:** The repo includes `.providers.json.example` and ignores `.providers.json` in `.gitignore`. Use the example as a reference, but make sure your real `.providers.json` is valid JSON, because the loader reads it with `json.loads()` and the example file contains inline comments for documentation purposes.

## Container Builds

Use the checked-in `Dockerfile` when you want a clean, reproducible runtime that matches the repository's containerized execution path. The image is based on Fedora 41, installs dependencies with `uv sync --locked`, and defaults to a safe collection-only pytest command.

```dockerfile
FROM quay.io/fedora/fedora:41

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1

ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

CMD ["uv", "run", "pytest", "--collect-only"]
```

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
