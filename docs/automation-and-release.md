# Automation And Release

Most of the automation in `mtv-api-tests` is configuration-driven. The repository tells tools what to check, how to build the test image, how to cut a release, how dependencies
should be updated, and how pull requests should be reviewed. What it does not include is the CI/CD pipeline definition that decides when those things run.

> **Note:** No GitHub Actions workflows, `Jenkinsfile`, Tekton definitions, or `.gitlab-ci.yml` files are present in this repository. Treat the repo as the source of automation
> policy, not as the orchestration layer.

## What Is Automated Here

- `pre-commit` enforces repository hygiene, Python linting and formatting, type checking, markdown linting, secret scanning, and a set of
  local AST checks that turn `AGENTS.md` rules into blocking hooks.
- `release-it` handles version bumping, commit and tag creation, pushing, changelog generation, and GitHub release creation.
- Renovate manages dependency update PRs and weekly lock-file maintenance.
- The `Dockerfile` defines a repeatable, multi-stage container build for the test suite.
- CodeRabbit and Qodo Merge/PR-Agent automate pull request review.
- `pytest` and `tox` expose CI-friendly entry points and artifacts such as JUnit XML.
- The `mtv-api-tests` Typer CLI generates a providers file and an OpenShift Job manifest, then runs tests locally or as a Job.
- rootcoz post-processes failed JUnit XML with AI analysis when `--analyze-with-ai` is used.
- `pi-docsite` builds the documentation site in `docs/` from Markdown sources.

There is one more marker file worth knowing about: `MTV-VERSION` is a tracked plain-text file holding the MTV release this suite is currently
aligned with (currently `2.12`). No code in the repository reads it, so changing it is a human decision, not a mechanical one.

## Pre-commit Quality Gates

The first automation layer is `.pre-commit-config.yaml`. It combines generic repository safety checks with Python tooling, security scanning,
and repo-specific AST checks, so a single `pre-commit` run catches a lot of problems early.

```1:119:.pre-commit-config.yaml
---
ci:
    autofix_prs: false
    autoupdate_commit_msg: "chore: pre-commit autoupdate"
    autoupdate_schedule: weekly

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
    rev: 7.4.1
    hooks:
      - id: flake8
        args: [--config=.flake8]
        additional_dependencies: [flake8-mutable]

  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        exclude: ^docs/

  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.9
    hooks:
      - id: ruff
      - id: ruff-format

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.0
    hooks:
      - id: gitleaks
        exclude: ^docs/

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

  - repo: local
    hooks:
      - id: check-no-kubernetes-runtime
        name: ban kubernetes runtime imports (except kubernetes.dynamic.exceptions and from kubernetes.dynamic import exceptions)
        entry: scripts/hooks/check_no_kubernetes_runtime.py
        language: python
        types: [python]
      - id: check-no-dynamicclient-construct
        name: ban DynamicClient() construction
        entry: scripts/hooks/check_no_dynamicclient_construct.py
        language: python
        types: [python]
      - id: check-no-except-exception
        name: ban except Exception (except pytest_* in conftest.py)
        entry: scripts/hooks/check_no_except_exception.py
        language: python
        types: [python]
      - id: check-no-module-load-source-providers
        name: ban module-level load_source_providers in tests/
        entry: scripts/hooks/check_no_module_load_source_providers.py
        language: python
        types: [python]
        files: ^tests/
      - id: check-exceptions-location
        name: require Exception subclasses in exceptions/exceptions.py
        entry: scripts/hooks/check_exceptions_location.py
        language: python
        types: [python]
      - id: check-test-file-location
        name: ban test_*.py directly under tests/
        entry: scripts/hooks/check_test_file_location.py
        language: python
        types: [python]
        files: ^tests/
      - id: check-autouse-fixtures
        name: only autouse_fixtures may use autouse=True
        entry: scripts/hooks/check_autouse_fixtures.py
        language: python
        types: [python]
      - id: check-no-runtimeerror
        name: ban raise RuntimeError (except pytest_* in conftest.py)
        entry: scripts/hooks/check_no_runtimeerror.py
        language: python
        types: [python]
```

## AGENTS.md Enforcement Hooks

The `repo: local` block is the most distinctive part of this file. Each of those eight hooks is a small AST-based script under
`scripts/hooks/` that turns one "MUST" rule from `AGENTS.md` into a blocking check.

| Hook id | Enforces |
| --- | --- |
| `check-no-kubernetes-runtime` | No `kubernetes.*` runtime imports, except `kubernetes.dynamic.exceptions` and `from kubernetes.dynamic import exceptions` |
| `check-no-dynamicclient-construct` | No `DynamicClient()` construction; use `get_client()` instead |
| `check-no-except-exception` | No `except Exception`, except in `pytest_*` functions in `conftest.py` |
| `check-no-module-load-source-providers` | No module-level `load_source_providers()` calls under `tests/` |
| `check-exceptions-location` | `Exception` subclasses belong in `exceptions/exceptions.py` |
| `check-test-file-location` | No `test_*.py` directly under `tests/` |
| `check-autouse-fixtures` | Only the `autouse_fixtures` fixture in `conftest.py` may declare `autouse=True` |
| `check-no-runtimeerror` | No `raise RuntimeError`, except in `pytest_*` functions in `conftest.py` |

Two of them are scoped further with `files: ^tests/`, so they only run on files under `tests/`.

Run the whole suite, one hook, or one script directly:

```bash
pre-commit run --all-files
pre-commit run check-no-except-exception --all-files
.venv/bin/python scripts/hooks/check_no_except_exception.py
```

### Baseline Ratchet

Four of these hooks still fail on legacy code. Rather than blocking every contribution, those findings are grandfathered in
`scripts/hooks/baselines/<hook_id>.txt`. The current baselines are:

- `check_exceptions_location.txt`
- `check_no_except_exception.txt`
- `check_no_kubernetes_runtime.txt`
- `check_no_runtimeerror.txt`

The filename is the snake_case script stem, not the kebab-case pre-commit id: `check_no_except_exception.py` maps to
`check_no_except_exception.txt`.

Each baseline line is `relative/posix/path:<16-char-sha256-hex>`, where the fingerprint is `sha256(span)[:16]`. For a single-line finding the
span is that line; for a multi-line AST node it is the joined source lines. Matching is on path plus content fingerprint with occurrence
counts, not on line number, so refactoring that moves a violation does not silently un-baseline it.

> **Tip:** Shrink a baseline row when you fix the underlying violation. Do not expand baselines casually; a new path/fingerprint pair that is not
> listed must fail the hook.
>
> **Warning:** Baselines are a ratchet, not an amnesty. Adding a line to a baseline is a deliberate reviewable act, not a way to get a red hook
> to go green.

## Generic Hook Behavior

That hook list tells you what the repo cares about:

- repository safety: large files, merge conflicts, broken symlinks, stray debug statements, invalid TOML, and missing EOF newlines
- Python quality: `flake8`, `ruff`, `ruff-format`, and `mypy`
- secret prevention: `detect-private-key`, `detect-secrets`, and `gitleaks`
- docs hygiene: `markdownlint-cli2 --fix`
- project rules: the eight `AGENTS.md` AST hooks listed above

The `ci:` block at the top pins the behavior of pre-commit.ci itself: `autoupdate_schedule: weekly` and `autofix_prs: false`. That means hook
revisions are bumped on a schedule, and no autofix PR is opened for you.

Three hooks carry `exclude: ^docs/`: `detect-secrets`, `gitleaks`, and `markdownlint-cli2`. The documentation tree is generated-site territory,
so it is excluded from linting and secret scanning by design.

> **Note:** Because `markdownlint-cli2` skips `docs/`, the Markdown under `docs/` is never auto-fixed and never fails CI. `.markdownlint.yaml`
> still documents the house rules (`MD013` at 180 characters, `MD033` allowing only `details`, `summary`, and `strong`), and they are what a
> reviewer expects those pages to follow.

The repo-specific behavior for `ruff` and `mypy` lives in `pyproject.toml`, so the hooks follow local rules rather than generic defaults.

```1:22:pyproject.toml
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

A few practical takeaways matter for contributors and CI maintainers:

- `ruff` is allowed to auto-fix code.
- `mypy` is configured to reject incomplete function definitions and implicit optional types.
- `flake8` is intentionally narrow here: `.flake8` selects `M511` and loads `flake8-mutable`.
- Markdown formatting can be auto-corrected during a hook run instead of being fixed manually.

Because the repo runs both `detect-secrets` and `gitleaks`, example files can contain `# pragma: allowlist secret` comments to keep fake credentials from being flagged. That is
why files such as `.providers.json.example` contain secret-looking placeholders with allowlist annotations.

`.gitleaksignore` holds the remaining accepted false positives, one `path:rule:line` per entry, so the scanners stay quiet on known example
credentials without disabling the scanners.

> **Warning:** Pre-commit hook environments are pinned to `python3.13`, while the project itself allows `>=3.12,<3.14` and the container image sets `UV_PYTHON=python3.12`. Make
> sure your local machine or CI runner can provide the hook interpreter.
>
> **Tip:** If you copy snippets out of example JSON-like files, remove `# pragma: allowlist secret` comments before using them as real JSON. Those comments exist for secret
> scanners, not for JSON parsers.

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

```27:31:pyproject.toml
[project]
requires-python = ">=3.12, <3.14"
name = "mtv-api-tests"
version = "5.0.0"
description = "MTV API Tests"
```

That is important because the release flow is Python-package-centric even though the release tool comes from the Node ecosystem. The `@release-it/bumper` plugin updates
`project.version`, and the `after:bump` hook runs `uv sync` so the environment and `uv.lock` stay aligned with the new release.

`pyproject.toml` also declares the console entry point that the CLI is installed from, and the wheel packages that get shipped:

```71:79:pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["cli", "utilities", "libs", "exceptions"]

[project.scripts]
mtv-api-tests = "cli.mtv_api_tests:app"
```

Two details are easy to miss:

- `requireBranch` is `false`, so the repo itself does not enforce a release branch policy
- `requireCommits` is `false`, so the repo itself does not require new commits before a release

If you want stricter rules, enforce them in your external release pipeline.

> **Warning:** The repository contains `.release-it.json`, but it does not contain `package.json`, `package-lock.json`, `yarn.lock`, or `pnpm-lock.yaml`. Your release runner must
> provide `release-it`, `@release-it/bumper`, and `GITHUB_TOKEN` from outside the repo.

## Dependency Updates With Renovate

Renovate behavior is defined in `renovate.json`:

```1:21:renovate.json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "baseBranches": ["main", "/^v\\d+\\.\\d+$/"],
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

`baseBranches` is the part that ties Renovate to the release flow: update PRs target `main` and any `v<major>.<minor>` maintenance branch, which is
exactly the branch shape `release-it` produces when `requireBranch` is `false`.

Because this project uses `uv` and checks in `uv.lock`, Renovate is not just bumping top-level requirements. It is also part of keeping the lock file fresh.

> **Tip:** Grouping everything under `python-deps` reduces PR noise, but it also means update PRs can be broader than a one-package-at-a-time workflow.

## Container Image Build Inputs

The container image is built from `Dockerfile`. It is a two-stage build: a builder stage that compiles dependencies, and a slim runtime
stage that carries only the runtime libraries.

```1:86:Dockerfile
# syntax=docker/dockerfile:1

# ------------------------------------------------------------------------------
# Stage 1: builder - install build deps, compile extensions, sync dependencies
# ------------------------------------------------------------------------------
FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb5bd336d8c400f6ee976a9d731beccf3351fa7a6f485dced24ae4af17 AS builder

ARG APP_DIR=/app
ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_NO_CACHE=1

RUN microdnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  gcc \
  clang \
  git \
  python3-devel \
  && microdnf clean all

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR ${APP_DIR}

COPY cli cli
COPY docs docs
COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini ./

RUN mkdir -p ${APP_DIR}/output

RUN uv sync --locked \
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-wrapper.git@${OPENSHIFT_PYTHON_WRAPPER_COMMIT}"; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-utilities.git@${OPENSHIFT_PYTHON_UTILITIES_COMMIT}"; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 -r rm -rfv \
  && rm -rf ${APP_DIR}/.cache

# ------------------------------------------------------------------------------
# Stage 2: runtime - clean image with only runtime dependencies
# ------------------------------------------------------------------------------
FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb5bd336d8c400f6ee976a9d731beccf3351fa7a6f485dced24ae4af17

ARG APP_DIR=/app

# Runtime shared libraries required by compiled Python extensions:
#   libxml2  - lxml
#   libcurl  - pycurl (libcurl-minimal from the base image provides libcurl.so)
#   openssl  - cryptography / TLS
RUN microdnf -y install \
  python3 \
  libxml2 \
  openssl \
  git \
  && microdnf clean all

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV JUNITFILE=${APP_DIR}/output/
ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_NO_CACHE=1
# Prevents Python from writing .pyc files to disk
ENV PYTHONDONTWRITEBYTECODE=1
# Ensures Python output is logged straight to the terminal (useful for OpenShift logs)
ENV PYTHONUNBUFFERED=1

WORKDIR ${APP_DIR}

COPY --from=builder ${APP_DIR} ${APP_DIR}

RUN chgrp -R 0 ${APP_DIR} && chmod -R g=u ${APP_DIR}

USER 1001

CMD ["uv", "run", "pytest", "--collect-only"]
```

This build has a few important characteristics:

- the base image is `registry.access.redhat.com/ubi10/ubi-minimal:10.1`, pinned to a digest, not a floating Fedora tag
- the runtime stage installs only `python3`, `libxml2`, `openssl`, and `git`; build tooling never reaches the shipped layer
- Python dependency installation is driven by `uv sync --locked`, so `uv.lock` matters
- the copied subset is explicit: `cli`, `docs`, `utilities`, `tests`, `libs`, `exceptions`, plus `README.md`, `pyproject.toml`, `uv.lock`, `conftest.py`, and `pytest.ini`
- the build can optionally swap in development commits of `openshift-python-wrapper` and `openshift-python-utilities`
- the image runs as non-root `USER 1001`, with `chgrp -R 0 && chmod -R g=u` for OpenShift's arbitrary-UID model
- `PYTHONUNBUFFERED=1` means pytest output reaches `oc logs` without buffering delays
- the default container command only performs test collection

That last point is intentional: a plain container run validates the test suite can be discovered, but it does not launch a real migration test job.

The repository's `.dockerignore` is minimal, so the effective build-input boundary is the `COPY` list in `Dockerfile`, not the ignore file. In other words, the image is controlled
more by what is explicitly copied than by what is excluded.

> **Tip:** `OPENSHIFT_PYTHON_WRAPPER_COMMIT` and `OPENSHIFT_PYTHON_UTILITIES_COMMIT` are practical escape hatches when you need to validate this test suite against unreleased
> helper-library commits.
>
> **Warning:** The default container command is `uv run pytest --collect-only`. If your CI job should run real tests, it must override `CMD` or pass an explicit command.

## AI Review Bots

This repository configures two PR review bots: CodeRabbit and Qodo Merge/PR-Agent. They both automate review, but they are wired differently.

### CodeRabbit

CodeRabbit is configured for assertive automatic review on non-draft PRs targeting `main`, and it can request changes.

```14:39:.coderabbit.yaml
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
    auto_pause_after_reviewed_commits: 0
    auto_incremental_review: true
    ignore_title_keywords:
      - "WIP"
    enabled: true
    drafts: false
    base_branches:
      - main
```

That configuration tells users a lot about expected review behavior:

- reviews are direct rather than gentle
- draft PRs are skipped
- PRs with `WIP` in the title are ignored for auto-review
- the bot can ask for changes
- review coverage is broader than just Python style, including security, YAML, shell, and Dockerfile checks

Above the `reviews` block the same file sets `language: en-US`, an explicit severity ladder in `tone_instructions` (`CRITICAL`, `HIGH`, `MEDIUM`,
`LOW`), and `early_access: true`. Below it, a `knowledge_base` block opts out of nothing, enforces `code_guidelines` sourced from `CLAUDE.md`, and
scopes `learnings`, `issues`, and `pull_requests` learning to `auto`. `auto_pause_after_reviewed_commits: 0` means CodeRabbit never stops
re-reviewing an open PR, and `chat.auto_reply: true` lets it answer PR comments.

CodeRabbit therefore points its knowledge-base and guideline logic at `CLAUDE.md`, so it is expected to review against repository-specific rules
instead of only generic style advice.

### Qodo Merge / PR-Agent

Qodo Merge is configured in `.pr_agent.toml`:

```4:57:.pr_agent.toml
[config]
response_language = "en-US"
add_repo_metadata = true
add_repo_metadata_file_list = ["CLAUDE.md"]
ignore_pr_title = [
  "^\\[WIP\\]",
  "^WIP:",
  "^Draft:",
  "chore: pre-commit autoupdate",
]
ignore_pr_labels = ["wip", "work-in-progress"]

[github_app]
handle_push_trigger = true
pr_commands = ["/agentic_review", "/agentic_describe"]
push_commands = ["/agentic_review"]
handle_pr_actions = ["opened", "reopened", "ready_for_review"]
feedback_on_draft_pr = false

[review_agent]
comments_location_policy = "both"
inline_comments_severity_threshold = 1

[pr_reviewer]
extra_instructions = """
Review Style:
- Be direct and specific. Explain WHY rules exist.

Focus Areas:
- Python code quality (type annotations, exception handling)
- Security vulnerabilities (injection, credential exposure)
- YAML syntax validation
- Follow CLAUDE.md guidelines for project-specific standards
"""
require_security_review = true
require_tests_review = true
require_score_review = false

[pr_code_suggestions]
extra_instructions = "Focus on Python best practices, security, and maintainability. Follow CLAUDE.md standards."
focus_only_on_problems = false
suggestions_score_threshold = 5

[ignore]
# Generated by pi-docsite (see AGENTS.md "Documentation Site"). The Markdown
# sources (docs/*.md) and docs/nav.json stay in review scope on purpose.
glob = [
  "docs/*.html",
  "docs/assets/*",
  "docs/search-index.json",
  "docs/llms.txt",
  "docs/llms-full.txt",
]
regex = []
```

Compared to CodeRabbit, this config emphasizes command-driven interaction:

- GitHub App events trigger reviews on open, reopen, ready-for-review, and push
- reviewers ask for `/agentic_review` or `/agentic_describe`; pushes only answer `/agentic_review`
- `[review_agent]` posts inline comments at severity 1 and up, in both the diff and a summary
- security review and tests review are required focus areas
- WIP and draft states are intentionally ignored, and so is the automated `chore: pre-commit autoupdate` commit
- generated documentation artifacts are excluded from review by glob, while `docs/*.md` and `docs/nav.json` stay in scope

> **Note:** Both bot configs pull repository-specific guidance from `CLAUDE.md`. They are meant to reinforce the repo's own standards, not replace human ownership or branch policy.

## AI Failure Analysis With rootcoz

There is a separate, opt-in AI feature inside the pytest plugin, and it is unrelated to the PR review bots above. `conftest.py` adds
`--analyze-with-ai`, and `utilities/pytest_utils.py` POSTs the JUnit XML to a rootcoz server's `/analyze-failures` endpoint and writes the
enriched XML back over the same file. That is test-report post-processing, not a pull-request review bot.

```462:498:utilities/pytest_utils.py
def enrich_junit_xml(session: pytest.Session) -> None:
    """Read JUnit XML, send to server for analysis, write enriched XML back.
    ...
    """
    ...
    server_url = os.environ["ROOTCOZ_SERVER_URL"]
    raw_xml = xml_path.read_text()

    try:
        timeout_value = int(os.environ.get("ROOTCOZ_TIMEOUT", "600"))
    except ValueError:
        LOGGER.warning("Invalid ROOTCOZ_TIMEOUT value, using default 600 seconds")
        timeout_value = 600

    # Optional overrides; when omitted, rootcoz uses .rootcoz/settings.json
    payload: dict[str, str] = {"raw_xml": raw_xml}
    if ai_provider := os.environ.get("ROOTCOZ_AI_PROVIDER"):
        payload["ai_provider"] = ai_provider
    if ai_model := os.environ.get("ROOTCOZ_AI_MODEL"):
        payload["ai_model"] = ai_model
```

The client half of the contract is four environment variables: `ROOTCOZ_SERVER_URL` (required), `ROOTCOZ_TIMEOUT` (default `600`), and the
optional `ROOTCOZ_AI_PROVIDER` / `ROOTCOZ_AI_MODEL` overrides. This repository also carries `.rootcoz/settings.json`, which is read by the
rootcoz server and CLI, not by pytest: it pins the AI provider and model, an AI call timeout, peer AI configs, and the `additional_repos`
rootcoz may consult for this project.

Enrichment is skipped when the session exit code is `0`, and it is force-disabled under `--collect-only` and `--setup-plan`. The original JUnit
file is preserved if the call fails.

> **Note:** If you operate rootcoz yourself, `ROOTCOZ_SERVER_URL` plus the `.rootcoz/settings.json` in this repo are the whole setup. If you do
> not, the feature is simply off and nothing changes about your test run.

## Documentation Site Rebuild

The `docs/` tree is a [pi-docsite](https://pypi.org/project/pi-docsite/) project. Only the Markdown sources and `docs/nav.json` are hand-edited;
everything else in `docs/` is generated and committed.

| Path | Role |
| --- | --- |
| `docs/*.md` | Sources — the only files humans edit |
| `docs/nav.json` | Sidebar layout: groups and page order |
| `docs/*.html`, `docs/assets/`, `docs/search-index.json`, `docs/llms.txt`, `docs/llms-full.txt` | Generated — never hand-edit |

Rebuild after any change to a `docs/*.md` file or to `docs/nav.json`:

```bash
uvx pi-docsite --docs-dir docs \
  --tagline "Pytest-based integration suite for validating Migration Toolkit for Virtualization migrations into OpenShift Virtualization."
```

The rules that matter for automation:

- edit the Markdown, never the generated HTML; fix the source and rebuild
- generated files are committed, so a source change and its rebuilt output belong in the same commit
- a new page needs a `docs/<slug>.md` with exactly one H1 and a matching `nav.json` entry; a slug with no `.md` behind it is a build error
- output is byte-for-byte reproducible, so churn in untouched files means the generator changed, not a stale build
- search only works when `docs/` is served over HTTP, because `search-index.json` is fetched with `fetch()`

This is also why `.pr_agent.toml` carries an `[ignore]` block for the generated artifacts and why three pre-commit hooks exclude `^docs/`. The
generated files are real deliverables, but they are never a review or lint target.

## What External CI/CD Must Do

Because orchestration lives outside this repository, your CI/CD platform needs to call the repo's entry points explicitly. The repo already provides a good integration contract
for that.

`pytest.ini` makes test output CI-friendly by default, especially through JUnit XML generation:

```1:38:pytest.ini
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
    remote: Remote cluster migration tests
    warm: Warm migration tests
    copyoffload: Copy-offload (XCOPY) tests
    copyoffload_sanity: Copy-offload sanity tests - core scenarios for quick validation
    copyoffload_snapshots: Copy-offload snapshot tests (vSphere only)
    shared_disk: Shared disk migration tests (vSphere only)
    incremental: marks tests as incremental (xfail on previous failure)
    min_mtv_version: mark test to require minimum MTV version (e.g., @pytest.mark.min_mtv_version("2.6.0"))
    ova: OVA provider-specific tests
    vsphere: vSphere provider-specific tests
    esxi: ESXi provider-specific tests
    rhv: RHV provider-specific tests
    hyperv: Hyper-V provider-specific tests
    openstack: OpenStack provider-specific tests
    openshift: OpenShift provider-specific tests
    deep_inspection: Deep Inspection / Conversion CR tests (vSphere only)
    ca_crt: CA certificate field (ca.crt) provider secret tests
    aap: AAP (Ansible Automation Platform) hook integration tests (vSphere only)
    upgrade: MTV operator upgrade tests

junit_logging = all
```

That default configuration is designed for automation consumers:

- JUnit XML is always generated as `junit-report.xml`, with `junit_logging = all` so the logs ride along
- marker handling is strict, so an unregistered marker fails the run rather than silently running everything
- xdist is configured with `--dist=loadscope`, so parallel workers group by class
- `testpaths = tests`, so a bare `pytest` never picks up the docs or tooling directories

`--jira` is deliberately absent from `addopts`. `pytest-jira` is installed and `jira.cfg.example` exists, but Jira reporting is opt-in per run.

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
4. generate the Job manifest with `mtv-api-tests generate` if you want cluster-side isolation, or pass `--tc=` flags directly
5. run real, cluster-backed test jobs with the required provider credentials and OpenShift access
6. collect `junit-report.xml` as an artifact
7. optionally enable `--analyze-with-ai` if you operate a rootcoz server
8. run `release-it` in a dedicated release job when you are ready to cut a version

`tox.toml` also carries an `unused-code` environment that runs `pyutils-unusedcode --exclude-function-prefixes pytest_`, which is useful as a
periodic dead-code sweep even though nothing in the repo depends on it.

The repo adds pytest switches in `conftest.py` too: `--analyze-with-ai`, `--providers-json`, `--skip-data-collector`, `--data-collector-path`,
`--skip-teardown`, and `--openshift-python-wrapper-log-debug`. Those are useful knobs for external jobs, but they are not pipeline definitions
by themselves.

## The `mtv-api-tests` CLI

`pyproject.toml` installs one console script, `mtv-api-tests`, backed by the Typer app in `cli/mtv_api_tests/`. It has exactly two subcommands.

| Command | What it does |
| --- | --- |
| `mtv-api-tests generate` | Interactive wizard that discovers datastores, VMs, ESXi hosts, and storage classes, then writes `.providers.json` and a Job manifest |
| `mtv-api-tests run` | Runs the tests. `--mode local` shells out to `uv run pytest`; `--mode job` runs `oc apply -f` against the manifest |

Both accept `--category` (a pytest marker), and `run` accepts `--source-provider`, `--destination-provider`, `--storage-class`,
`--test-filter` / `-k`, and `--job-yaml`. `generate` additionally accepts `--image` for the Job container image.

Useful entry points for automation and debugging:

```bash
# Interactive configuration, writes .providers.json and the Job manifest
mtv-api-tests generate

# Run locally with explicit provider keys
mtv-api-tests run --mode local --source-provider <key> --destination-provider <key> --storage-class <sc>

# Deploy the previously generated manifest and follow its logs
mtv-api-tests run --mode job
```

In `local` mode the CLI passes the cluster host and SSL setting as `--tc=` arguments but the username and password as `CLUSTER_USERNAME` and
`CLUSTER_PASSWORD` environment variables, specifically so credentials do not appear in process listings. If no OpenShift provider is available it
falls back to the token from `oc whoami -t`.

Both commands read the providers file from `PROVIDERS_JSON_PATH`, falling back to `.providers.json` in the working directory — the same
resolution order pytest uses for `--providers-json`.

> **Tip:** The cleanest mental model is: this repository defines automation rules, while your CI/CD platform supplies the runner, credentials, scheduling, and environment needed
> to execute them.
