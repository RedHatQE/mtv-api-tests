# Automation And Release

Most of the automation in `mtv-api-tests` is configuration-driven. The repository tells tools what to check, how to build the test image, how to cut a release, how dependencies should be updated, and how pull requests should be reviewed. What it does not include is the CI/CD pipeline definition that decides when those things run.

> **Note:** No GitHub Actions workflows, `Jenkinsfile`, Tekton definitions, or `.gitlab-ci.yml` files are present in this repository. Treat the repo as the source of automation policy, not as the orchestration layer.

## What Is Automated Here

- `pre-commit` enforces repository hygiene, Python linting and formatting, type checking, markdown linting, and secret scanning.
- `release-it` handles version bumping, commit and tag creation, pushing, changelog generation, and GitHub release creation.
- Renovate manages dependency update PRs and weekly lock-file maintenance.
- The `Dockerfile` defines a repeatable container build for the test suite.
- CodeRabbit and Qodo Merge/PR-Agent automate pull request review.
- `pytest` and `tox` expose CI-friendly entry points and artifacts such as JUnit XML.

## Pre-commit Quality Gates

The first automation layer is `.pre-commit-config.yaml`. It combines generic repository safety checks with Python tooling and security scanning, so a single `pre-commit` run catches a lot of problems early.

```1:67:.pre-commit-config.yaml
---
default_language_version:
  python: python3.13

repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: check-added-large-files
      - id: check-docstring-first
      - id: check-executables-have-shebangs
      - id: check-merge-conflict
      - id: check-symlinks
      - id: detect-private-key
      - id: mixed-line-ending
      - id: debug-statements
      - id: trailing-whitespace
        args: [--markdown-linebreak-ext=md] # Do not process Markdown files.
      - id: end-of-file-fixer
      - id: check-ast
      - id: check-builtin-literals
      - id: check-docstring-first
      - id: check-toml

  - repo: https://github.com/PyCQA/flake8
    rev: 7.3.0
    hooks:
      - id: flake8
        args: [--config=.flake8]
        additional_dependencies: [flake8-mutable]

  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.4
    hooks:
      - id: ruff
      - id: ruff-format

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.0
    hooks:
      - id: gitleaks

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

That hook list tells you what the repo cares about:

- repository safety: large files, merge conflicts, broken symlinks, stray debug statements, invalid TOML, and missing EOF newlines
- Python quality: `flake8`, `ruff`, `ruff-format`, and `mypy`
- secret prevention: `detect-private-key`, `detect-secrets`, and `gitleaks`
- docs hygiene: `markdownlint-cli2 --fix`

The repo-specific behavior for `ruff` and `mypy` lives in `pyproject.toml`, so the hooks follow local rules rather than generic defaults.

```1:18:pyproject.toml
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
```

A few practical takeaways matter for contributors and CI maintainers:

- `ruff` is allowed to auto-fix code.
- `mypy` is configured to reject incomplete function definitions and implicit optional types.
- `flake8` is intentionally narrow here: `.flake8` selects `M511` and loads `flake8-mutable`.
- Markdown formatting can be auto-corrected during a hook run instead of being fixed manually.

Because the repo runs both `detect-secrets` and `gitleaks`, example files can contain `# pragma: allowlist secret` comments to keep fake credentials from being flagged. That is why files such as `.providers.json.example` contain secret-looking placeholders with allowlist annotations.

> **Warning:** Pre-commit hook environments are pinned to `python3.13`, while the project itself allows `>=3.12,<3.14` and the container image sets `UV_PYTHON=python3.12`. Make sure your local machine or CI runner can provide the hook interpreter.

> **Tip:** If you copy snippets out of example JSON-like files, remove `# pragma: allowlist secret` comments before using them as real JSON. Those comments exist for secret scanners, not for JSON parsers.

## Release Automation With `release-it`

Release configuration lives in `.release-it.json`. This repository uses `release-it` for Git and GitHub release operations, not for publishing an npm package.

```1:48:.release-it.json
{
  "npm": {
    "publish": false
  },
  "git": {
    "requireCleanWorkingDir": true,
    "requireBranch": false,
    "requireUpstream": true,
    "requireCommits": false,
    "addUntrackedFiles": false,
    "commit": true,
    "commitMessage": "Release ${version}",
    "commitArgs": [],
    "tag": true,
    "tagName": null,
    "tagMatch": null,
    "tagAnnotation": "Release ${version}",
    "tagArgs": [],
    "push": true,
    "pushArgs": ["--follow-tags"],
    "pushRepo": "",
    "changelog": "git log --no-merges --pretty=format:\"* %s (%h) by %an on %as\" ${from}...${to}"
  },
  "github": {
    "release": true,
    "releaseName": "Release ${version}",
    "releaseNotes": null,
    "autoGenerate": false,
    "preRelease": false,
    "draft": false,
    "tokenRef": "GITHUB_TOKEN",
    "assets": null,
    "host": null,
    "timeout": 0,
    "proxy": null,
    "skipChecks": false,
    "web": false
  },
  "plugins": {
    "@release-it/bumper": {
      "in": "pyproject.toml",
      "out": { "file": "pyproject.toml", "path": "project.version" }
    }
  },
  "hooks": {
    "after:bump": "uv sync"
  }
}
```

Here is what that means in practice:

- the release job must start from a clean working tree
- the branch must have an upstream remote
- `release-it` creates a release commit and tag, then pushes both with `--follow-tags`
- GitHub release creation is enabled, using `GITHUB_TOKEN`
- GitHub's auto-generated release notes are disabled
- changelog text is built from `git log --no-merges ... ${from}...${to}`

The version source is the Python project metadata in `pyproject.toml`:

```27:30:pyproject.toml
[project]
requires-python = ">=3.12, <3.14"
name = "mtv-api-tests"
version = "2.8.3"
```

That is important because the release flow is Python-package-centric even though the release tool comes from the Node ecosystem. The `@release-it/bumper` plugin updates `project.version`, and the `after:bump` hook runs `uv sync` so the environment and `uv.lock` stay aligned with the new release.

Two details are easy to miss:

- `requireBranch` is `false`, so the repo itself does not enforce a release branch policy
- `requireCommits` is `false`, so the repo itself does not require new commits before a release

If you want stricter rules, enforce them in your external release pipeline.

> **Warning:** The repository contains `.release-it.json`, but it does not contain `package.json`, `package-lock.json`, `yarn.lock`, or `pnpm-lock.yaml`. Your release runner must provide `release-it`, `@release-it/bumper`, and `GITHUB_TOKEN` from outside the repo.

## Dependency Updates With Renovate

Renovate behavior is defined in `renovate.json`:

```1:20:renovate.json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": [
    ":dependencyDashboard",
    ":maintainLockFilesWeekly",
    ":prHourlyLimitNone",
    ":semanticCommitTypeAll(ci )"
  ],
  "prConcurrentLimit": 0,
  "recreateWhen": "never",
  "lockFileMaintenance": {
    "enabled": true
  },
  "packageRules": [
    {
      "matchPackagePatterns": ["*"],
      "groupName": "python-deps"
    }
  ]
}
```

This gives the repo a clear dependency-update strategy:

- a dependency dashboard is enabled
- lock-file maintenance runs weekly
- Renovate is not throttled by an hourly PR cap
- concurrent PRs are unlimited
- closed PRs are not automatically recreated
- matched dependencies are grouped under `python-deps`

Because this project uses `uv` and checks in `uv.lock`, Renovate is not just bumping top-level requirements. It is also part of keeping the lock file fresh.

> **Tip:** Grouping everything under `python-deps` reduces PR noise, but it also means update PRs can be broader than a one-package-at-a-time workflow.

## Container Image Build Inputs

The container image is built from `Dockerfile`, and the file is very explicit about what goes into the image:

```1:47:Dockerfile
FROM quay.io/fedora/fedora:41

ARG APP_DIR=/app

ENV JUNITFILE=${APP_DIR}/output/

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_CACHE_DIR=${APP_DIR}/.cache

RUN dnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  libcurl-devel \
  gcc \
  clang \
  python3-devel \
  && dnf clean all \
  && rm -rf /var/cache/dnf \
  && rm -rf /var/lib/dnf \
  && truncate -s0 /var/log/*.log && rm -rf /var/cache/yum

WORKDIR ${APP_DIR}

RUN mkdir -p ${APP_DIR}/output

COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini ./

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

RUN uv sync --locked\
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-wrapper.git@$OPENSHIFT_PYTHON_WRAPPER_COMMIT; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-utilities.git@$OPENSHIFT_PYTHON_UTILITIES_COMMIT; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv \
  && rm -rf ${APP_DIR}/.cache

CMD ["uv", "run", "pytest", "--collect-only"]
```

This build has a few important characteristics:

- the base image is `quay.io/fedora/fedora:41`
- Python dependency installation is driven by `uv sync --locked`, so `uv.lock` matters
- only a specific subset of the repository is copied into the image
- the build can optionally swap in development commits of `openshift-python-wrapper` and `openshift-python-utilities`
- the default container command only performs test collection

That last point is intentional: a plain container run validates the test suite can be discovered, but it does not launch a real migration test job.

The repository's `.dockerignore` is minimal, so the effective build-input boundary is the `COPY` list in `Dockerfile`, not the ignore file. In other words, the image is controlled more by what is explicitly copied than by what is excluded.

> **Tip:** `OPENSHIFT_PYTHON_WRAPPER_COMMIT` and `OPENSHIFT_PYTHON_UTILITIES_COMMIT` are practical escape hatches when you need to validate this test suite against unreleased helper-library commits.

> **Warning:** The default container command is `uv run pytest --collect-only`. If your CI job should run real tests, it must override `CMD` or pass an explicit command.

## AI Review Bots

This repository configures two PR review bots: CodeRabbit and Qodo Merge/PR-Agent. They both automate review, but they are wired differently.

### CodeRabbit

CodeRabbit is configured for assertive automatic review on non-draft PRs targeting `main`, and it can request changes.

```14:72:.coderabbit.yaml
reviews:
  # Review profile: assertive for strict enforcement
  profile: assertive

  # Request changes for critical violations
  request_changes_workflow: true

  # Review display settings
  high_level_summary: true
  poem: false
  review_status: true
  collapse_walkthrough: false

  # Abort review if PR is closed
  abort_on_close: true

  # Auto-review configuration
  auto_review:
    auto_incremental_review: true
    ignore_title_keywords:
      - "WIP"
    enabled: true
    drafts: false
    base_branches:
      - main

  # Enable relevant tools for Python/JavaScript project
  tools:
    # Python linting
    ruff:
      enabled: true
    pylint:
      enabled: true

    # JavaScript linting
    eslint:
      enabled: true

    # Shell script checking
    shellcheck:
      enabled: true

    # YAML validation
    yamllint:
      enabled: true

    # Security scanning
    gitleaks:
      enabled: true
    semgrep:
      enabled: true

    # GitHub Actions workflow validation
    actionlint:
      enabled: true

    # Dockerfile linting
    hadolint:
      enabled: true
```

That configuration tells users a lot about expected review behavior:

- reviews are direct rather than gentle
- draft PRs are skipped
- PRs with `WIP` in the title are ignored for auto-review
- the bot can ask for changes
- review coverage is broader than just Python style, including security, YAML, shell, and Dockerfile checks

CodeRabbit also points its knowledge-base and guideline logic at `CLAUDE.md`, so it is expected to review against repository-specific rules instead of only generic style advice.

### Qodo Merge / PR-Agent

Qodo Merge is configured in `.pr_agent.toml`:

```4:47:.pr_agent.toml
[config]
response_language = "en-US"
add_repo_metadata = true
add_repo_metadata_file_list = ["CLAUDE.md"]
ignore_pr_title = ["^\\[WIP\\]", "^WIP:", "^Draft:"]
ignore_pr_labels = ["wip", "work-in-progress"]

[github_app]
handle_pr_actions = ["opened", "reopened", "ready_for_review"]
pr_commands = ["/describe", "/review", "/improve"]
feedback_on_draft_pr = false
handle_push_trigger = true
push_commands = ["/review", "/improve"]

[pr_reviewer]
extra_instructions = """
Review Style:
- Be direct and specific. Explain WHY rules exist.
- Classify each finding by severity:
  * CRITICAL: Security vulnerabilities, blocking issues - must fix before merge
  * HIGH: Type errors, defensive programming issues - should fix
  * MEDIUM: Style/code quality issues - nice to fix
  * LOW: Suggestions/optional enhancements

Focus Areas:
- Python code quality (type annotations, exception handling)
- Security vulnerabilities (injection, credential exposure)
- YAML syntax validation
- Follow CLAUDE.md guidelines for project-specific standards
"""
require_security_review = true
require_tests_review = true
require_estimate_effort_to_review = true
require_score_review = false
enable_review_labels_security = true
enable_review_labels_effort = true
num_max_findings = 50
persistent_comment = false
enable_help_text = true

[pr_code_suggestions]
extra_instructions = "Focus on Python best practices, security, and maintainability. Follow CLAUDE.md standards."
focus_only_on_problems = false
suggestions_score_threshold = 5
```

Compared to CodeRabbit, this config emphasizes command-driven interaction:

- GitHub App events trigger reviews on open, reopen, ready-for-review, and push
- reviewers can explicitly ask for `/describe`, `/review`, or `/improve`
- security review and tests review are required focus areas
- WIP and draft states are intentionally ignored

> **Note:** Both bot configs pull repository-specific guidance from `CLAUDE.md`. They are meant to reinforce the repo's own standards, not replace human ownership or branch policy.

There is also a separate, opt-in AI feature inside the pytest plugin. `conftest.py` adds `--analyze-with-ai`, and `utilities/pytest_utils.py` uses `JJI_SERVER_URL`, `JJI_AI_PROVIDER`, and `JJI_AI_MODEL` to enrich JUnit XML through an external `/analyze-failures` service. That is test-report post-processing, not a pull-request review bot.

## What External CI/CD Must Do

Because orchestration lives outside this repository, your CI/CD platform needs to call the repo's entry points explicitly. The repo already provides a good integration contract for that.

`pytest.ini` makes test output CI-friendly by default, especially through JUnit XML generation:

```1:25:pytest.ini
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

markers =
    tier0: Core functionality tests (smoke tests)
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))

junit_logging = all
```

That default configuration is designed for automation consumers:

- JUnit XML is always generated as `junit-report.xml`
- marker handling is strict
- xdist is configured with `--dist=loadscope`
- logging is enabled in a CI-readable way

The repo also ships a small `tox` surface for automation:

```1:26:tox.toml
skipsdist = true
env_list = ["pytest-check", "unused-code"]

[env.pytest-check]
commands = [
  [
    "uv",
    "run",
    "pytest",
    "--setup-plan",
  ],
  [
    "uv",
    "run",
    "pytest",
    "--collect-only",
  ],
]


description = "Run pytest collect-only and setup-plan"
deps = ["uv"]
[env.unused-code]
description = "Find unused code"
deps = ["python-utility-scripts"]
commands = [["pyutils-unusedcode", "--exclude-function-prefixes", "pytest_"]]
```

That means an external pipeline can use the repository in layers:

1. run `pre-commit run --all-files`
2. run `tox -e pytest-check` or `uv run pytest --collect-only` as a fast wiring check
3. build the image from `Dockerfile`
4. run real, cluster-backed test jobs with the required provider credentials and OpenShift access
5. collect `junit-report.xml` as an artifact
6. optionally enable `--analyze-with-ai` if you operate the external analysis service
7. run `release-it` in a dedicated release job when you are ready to cut a version

The repo also adds extra pytest switches in `conftest.py`, including `--skip-data-collector`, `--skip-teardown`, `--openshift-python-wrapper-log-debug`, and `--analyze-with-ai`. Those are useful knobs for external jobs, but they are not pipeline definitions by themselves.

> **Tip:** The cleanest mental model is: this repository defines automation rules, while your CI/CD platform supplies the runner, credentials, scheduling, and environment needed to execute them.
