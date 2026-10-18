# MTV API Tests — Agent Instructions

## About This File

This file is a **budgeted index**. Its line count is capped by the `check-agents-md-budget`
pre-commit hook (shrink-only — growing past the cap fails the commit; raising the cap requires a
deliberate edit of the hook's constant).

- Rules live here in minimal form. Examples and encyclopedic detail live in the linked files
  (`test_plan_schema.md`, `docs/`, hook sources) — link to them, never paste their content here.
- No duplications: define information once, reference it elsewhere.
- Clear, unambiguous instructions — no vague terms without definitions.
- Update this file when an agent makes a mistake; prune stale rules — outdated rules are actively
  harmful because agents trust them over the actual code. Changes need user approval before commit.

## Commands

| Task | Command |
| --- | --- |
| Install dependencies | `uv sync` (never pip) |
| Lint/format gate (MUST pass before commit, never `--no-verify`) | `pre-commit run --all-files` |
| Build container | `podman build -f Dockerfile -t mtv-api-tests` |
| Check test coverage before writing a test | `uv run mtv-api-tests coverage <keywords>` |
| Regenerate docs site | `uvx pi-docsite --docs-dir docs --tagline "Pytest-based integration suite for validating Migration Toolkit for Virtualization migrations into OpenShift Virtualization."` |

## Stack

Python 3.13, `uv`, pytest, `openshift-python-wrapper` for all OpenShift/Kubernetes interaction.
Runtime `kubernetes` package usage is banned (hook-enforced: `check-no-kubernetes-runtime`,
`check-no-dynamicclient-construct`). Type annotations required on all new/changed function
signatures — built-in typing (`dict`, `list`); `Any` must be justified.

## AI Workflow

1. **User Prompt** — fix/new test/feature/enhancement
2. **Branch** — feature branch (`feat/`, `fix/`, `refactor/`, `docs/`)
3. **Agent Selection** — route to specialist agents, in parallel when possible
4. **Code Changes** — specialist implements
5. **Code Review** — delegate to `code-reviewer` after ANY code change; repeat 4-5 until clean
6. **Pre-commit** — `pre-commit run --all-files`, fix failures (no re-review needed for formatting)
7. **Completion** — reviewed, hooks green, ready to commit

Never skip the reviewer after code changes. Do not dismiss review comments without a resolution
comment; if out of scope, create a GitHub issue and reference it.

## Documentation Sync (MUST)

Code changes and new code MUST keep documentation in sync — drift is a bug:

| Change | Must update |
| --- | --- |
| Affects usage/requirements/installation/configuration | `README.md` |
| Changes methodology or coding patterns | This file (AGENTS.md/CLAUDE.md), user approval first |
| Any code change or new code | The relevant `docs/*.md` page(s) |
| Any `docs/*.md` or `docs/nav.json` edit | Regenerate the site (command above) and commit generated output together with the source change |

The `docs/` folder is a [pi-docsite](https://pypi.org/project/pi-docsite/) project:

- `docs/*.md` and `docs/nav.json` are the only files humans edit. `docs/*.html`, `docs/assets/`,
  `docs/search-index.json`, `docs/llms.txt`, `docs/llms-full.txt` are generated — never hand-edit;
  fix the Markdown and rebuild.
- New page: write `docs/<slug>.md` with exactly one H1, add one line to `docs/nav.json`, rebuild.
  Reorder via `nav.json`, never by renaming files. A `nav.json` slug with no `.md` is a build error.
- Cross-page links target `.html`, not `.md` (pi-docsite does not rewrite link targets).
- Rebuild only after all `docs/*.md` edits are finished; verify with two consecutive identical builds.
- Generated files are committed. The generator is deliberately unpinned — an upstream release may
  regenerate many pages with no source change: commit that alone as
  `chore(docs): regenerate site after upstream generator release`, never mixed into a doc change.

## Code Standards

Enforced rules are marked (hook). Full reasoning for each rule lives with its enforcing hook or in
`docs/extending-the-suite.md`.

- **No auto-skip:** never `pytest.skip()`/`pytest.fail()` for validation inside fixtures or tests.
  Validation (required inputs/config) belongs in fixtures; assertions (outcomes) in test methods.
  Conditional skipping: `@pytest.mark.skipif` at class/test level; provider-capability gating on
  config happens at collection time in `pytest_collection_modifyitems`.
- **No inline imports** (ruff `PLC0415`): imports at file top; type-only imports in
  `TYPE_CHECKING` blocks are fine.
- **DynamicClient:** never instantiate directly — `get_client()` from `ocp_utilities.infra` (hook).
- **Use existing libraries** — do not reimplement what dependencies provide:

| Need | Use |
| --- | --- |
| OpenShift resources | `ocp_resources.*` (openshift-python-wrapper) |
| SSH commands | `python-rrmngmnt` |
| Shell commands | `pyhelper-utils` `run_ssh_commands` |
| VM inventory data | `ForkliftInventory` |

  Upstream missing a feature (e.g. a Plan status)? Open a PR to the upstream library and use it
  after merge — no hardcoded workarounds.

- **Function size:** single responsibility (if the docstring needs "and", split); under 50 lines
  when possible; `_`-prefixed helpers for sub-tasks; names say WHAT (`is_warm_migration_supported`).
- **Fail fast:** code must never let `None` propagate where it is invalid — raise with a clear
  error. `if firmware is None:` for None checks; `if not labels:` only when empty or False are
  also invalid.
- **Pass objects over values:** functions receive objects and extract values internally (when you
  control both signature and caller).
- **Consistent variable types:** `actual_affinity: dict[str, Any] = vm.get("affinity") or {}` —
  the container type is fixed, but an external provider's value shape is not, which is the
  justified use of `Any` here.
- **Trust required arguments:** never check that required args were passed. "Validate at Source":
  fixtures validate the VALUE they produce.
- **No duplicate code:** same logic twice → extract a function. Same expression computed twice →
  store in a variable. No abstractions for single-use or merely similar code.
- **No unnecessary variables:** `yield create_resource()`, not `result = ...; yield result`.
- **Exception types:** specific over generic. Invalid input/missing resource → `ValueError`; type
  issues → `TypeError`; connection → `ConnectionError`; domain errors → custom exception class in
  `exceptions/exceptions.py` (centralized — hook: `check-exceptions-location`). `RuntimeError` only
  in pytest hooks for infrastructure failures (hook: `check-no-runtimeerror`).
- **No `except Exception`** (hook: `check-no-except-exception`) — it masks bugs. Catch specific
  exceptions; re-wrap into generic ones is also a violation. Catching to log-and-continue is a
  violation.
- **No fallbacks, no silent recovery:** when something is wrong, fail immediately. `except` +
  alternative path, `LOGGER.warning` papering over a failure — all violations.
- **No unnecessary logging:** pytest captures output. Progress logging for long operations
  (migration, provider connection) is acceptable; redundant per-step logging is not.
- **No dead code or useless comments:** nothing that restates the code, `# Success!`,
  unreachable code, commented-out blocks, debug leftovers, `type: ignore` without justification.
- **Do not mutate caller data** in-place unless documented as the purpose — work on a copy.
- **Magic numbers/hardcoded values:** named constants with a comment (`_SERIAL_PORT_OFFSET = 4000`).
- **`isinstance()` over `hasattr`/duck-typing** — a typo in `hasattr` silently skips logic.
- **No removed functionality without justification** in the PR/commit message — especially
  validations, error handling, and changed defaults.
- **Feature-specific logic must not pollute general code:** no `if copyoffload:` branches inside
  general-purpose functions; feature functions stay separate.
- **Empty container defaults** for optional/nested external data
  (`template.spec.domain.get("firmware", {})`); fail fast for required data. See also
  "Deterministic Tests" below for our own config.
- **Docstring format (MUST):** all new functions get docstrings — `Args` when there are params;
  `Returns` only for a meaningful non-`None` value — **void functions omit `Returns`; never add
  "Returns: None" to satisfy an external linter** (cite this rule in review replies); `Raises` only
  when exceptions are raised.
- **Context managers for cleanup:** `with ResourceEditor(node) as editor:` so cleanup is guaranteed.

## Where Code Lives (MUST)

Every piece of code has exactly one correct location — misplacement is a review blocker:

| Code type | Location |
| --- | --- |
| Pytest fixtures and hooks | `conftest.py` closest to the tests that use them |
| Helper/utility functions | `utilities/<module>.py` |
| Provider-specific logic | `libs/providers/<provider>.py` |
| Constants | Top of the owning module, after imports |
| Custom exceptions | `exceptions/exceptions.py` |
| Test files | `tests/<feature>/` only — never directly under `tests/` (hook: `check-test-file-location`) |

No unit tests, no standalone test scripts, no mocks — tests exercise MTV product behavior
end-to-end against live infrastructure.

## Test Authoring

### Workflow — follow in order

1. **Check coverage first:** `uv run mtv-api-tests coverage <keywords>` (add `--json` for machine
   output, `--missing-plans` for the retrofit audit). If a class already covers the scenario,
   extend it instead of adding a near-duplicate.
2. **Config:** add a `tests_params` entry in `tests/tests_config/config.py`.
3. **File:** `tests/<feature>/test_<feature>_migration.py`.
4. **Class:** apply markers, then write the plan docstring **before** the methods, per
   [`test_plan_schema.md`](test_plan_schema.md). Pre-commit enforces it
   (`check-test-plan-docstring-schema`, no baseline, no grandfathering — any docstring edit
   re-validates; a docless or non-conforming class fails the commit).
5. **Methods:** match the step pattern of neighboring classes (find them with the coverage CLI).

### Step patterns

Base: `test_create_storagemap` → `test_create_networkmap` → `test_create_plan` →
`test_migrate_vms` → `test_check_vms`. Feature variants insert their step(s) before
`test_check_vms`:

| Pattern (marker/naming) | Steps |
| --- | --- |
| Shared disk, Linux (`shared_disk`) | + `test_verify_shared_disk_data` before `check_vms` |
| Shared disk, Windows | + `test_label_shared_disk` first, then shared-disk Linux steps |
| Copy-offload (`copyoffload`) | + `test_check_xcopy_used` before `check_vms` |
| Copy-offload throttling | + `test_verify_populator_throttling`, then `check_xcopy_used` |
| Copy-offload VM+populator throttling | + `test_verify_vm_inflight_throttling`, then throttling steps |
| Copy-offload dedicated host | + `test_verify_dedicated_migration_host`, then `check_xcopy_used` |
| LUKS | + `test_verify_luks_encryption` |
| XFS (`xfs_compatibility: True`) | + `test_verify_xfs_version` |
| Plan-archive PVC cleanup (`tests/plan_lifecycle/`) | migrate (expected `MigrationPlanExecError`) → `test_archive_and_delete_plan` → `test_verify_pvc_cleanup`; no `check_vms` |
| Resume-conversion (`vsphere`) | migrate (expected fail) → `test_verify_pvcs_preserved` → `test_resume_migration` → `check_vms` |
| Plan-readiness (4-step) | verify_\<feature\> → storagemap → networkmap → plan; no migration |

### Example class

```python
@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_name_here"])],
    indirect=True,
    ids=["descriptive-test-id"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
@pytest.mark.tier0
class TestNameHere:
    """[One-sentence product scenario.]

    Purpose/Regression:
        [Behavior or regression under test, why it matters, how failure is induced.]

    Prerequisites:
        [Provider registration, disposable VM characteristics, destination
        storage/network, permissions, feature settings, isolation.]

    Test plan:
        1. [Prepare resources and confirm source inventory visibility.]
        2. [Create StorageMap and NetworkMap; create the Plan and confirm Ready.]
        3. [Execute the migration and observe its status.]
        4. [Verify the migrated VMs match the expected configuration.]
        5. [Clean up the created resources.]

    Expected result:
        [Observable pass criteria and product evidence to inspect on failure.]
    """

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(self, ...):
        """Create StorageMap resource."""
        ...

    def test_create_networkmap(self, ...):
        """Create NetworkMap resource."""
        ...

    def test_create_plan(self, ...):
        """Create MTV Plan CR resource."""
        ...

    def test_migrate_vms(self, ...):
        """Execute migration."""
        ...

    def test_check_vms(self, ...):
        """Validate migrated VMs."""
        ...
```

The docstring above is the structural template — replace every bracketed placeholder with the real
scenario content; placeholder brackets left as-is will not survive review. Method signatures and
imports are elided (`...`) — request only the fixtures each method needs.
`@pytest.mark.incremental` gives sequential test dependencies; shared state lives on the class
(`self.__class__.attribute`).

### VM configuration options (`tests_params` per-VM dicts)

| Option | Required | Values / Notes |
| --- | --- | --- |
| `name` | Yes | VM name in source provider |
| `source_vm_power` | No | "on" / "off" |
| `guest_agent` | No | True if installed |
| `clone` | No | True to clone before migration |
| `disk_type` | No | "thin" / "thick-lazy" / "thick-eager" |
| `luks` / `luks_passphrase` | No | LUKS-encrypted disk; per-VM passphrase override |
| `migrate_shared_disks` | No | True for owner VM in shared-disk tests |
| `add_nic` | No | True to add a NIC (vSphere only) |
| `add_nic_start_connected` | Yes* | Required when `add_nic=True`; bool |
| `win_os` | No | Hyper-V guest OS override (bool) |

### Plan configuration options

| Option | Effect when `True` |
| --- | --- |
| `warm_migration` | Warm migration |
| `preserve_static_ips` | Preserve static IPs after migration |
| `copyoffload` | Copy-offload (XCOPY) migration |
| `xfs_compatibility` | XFS v4 filesystem compatibility (requires `xfs_check` config: `command`, `mount_point`, `expected_output`) |
| `migrate_shared_disks` | Shared disk migration at plan level |
| `inventory_timeout` | Per-VM Forklift inventory wait timeout (seconds) |
| `clone_to_same_host` | VM2+ default to VM1's ESXi host; explicit `target_esxi_host` overrides |
| `pin_to_non_dedicated_host` | Pin every VM outside `copyoffload.dedicated_migration_hosts` |
| `disable_drs_for_vms` | Disable vSphere DRS per VM after cloning (not for OVA; needs VMware clone provider) |
| `per_nic_network_map` | Per-NIC network mappings (duplicate source networks allowed) |
| `skip_clone` | Skip VM cloning in `prepared_plan` (plan-readiness tests on existing VMs) |
| `rdm_as_lun` | Map RDM disks as LUN devices with SCSI bus |
| `pre_copy_source_disks` | Hyper-V two-phase VHDX→raw transfer (Hyper-V source only) |
| `network_map_overrides` | Per-network destination overrides chosen by `source_ip_config`; `pod`/`multus`/`ignored` + optional `network_ip_mode` |

The boolean flags in this table use `plan.get("flag", False)` in code. Exceptions:
`inventory_timeout` is numeric and defaults via `plan.get("inventory_timeout", 300)`;
`rdm_as_lun` is accessed directly (`prepared_plan["rdm_as_lun"]`) in the tests that set it.
Every other key in our config (`py_config`, `plan`, `tests_params`) is accessed directly — see
"Deterministic Tests".

## Test-Plan Docstring Accuracy

Plans describe what the test **actually does** — verified against the class's config keys, the
utilities it calls, and `check_vms()`'s provider-conditional gating (static-IP assertions run only
for vSphere/Hyper-V; NIC-name assertions only for vSphere). Asserted is not inferred: a migration
reporting `Succeeded` does not prove a hook ran — say which is checked and how. The full
human-review checklist is in [`test_plan_schema.md`](test_plan_schema.md).

## Linter Design Rules

- New hooks **never** get baselines or suppression lists: `--all-files` means the current state of
  every file, always.
- Sanctioned exception: the issue-#610 ratchets in `scripts/hooks/baselines/`
  (`check_no_except_exception`, `check_no_kubernetes_runtime`, `check_no_runtimeerror`,
  `check_exceptions_location`) — shrink when fixing, never expand.
- One rule = one hook.
- `test_plan_schema.md`'s **machine-checkable structure** (sections, order, numbered steps) is
  implemented twice — in `scripts/hooks/check_test_plan_docstring_schema.py` and in the coverage
  CLI's `conforming` check (`cli/mtv_api_tests/coverage.py`). Changing the structure updates all
  three together. The accuracy checklist in the same file is human-review scope by design — it is
  not mechanically enforceable.

## Critical Constraints

- **No rebase on PR branches (MUST):** never rebase or rewrite history on a PR branch; force-push
  is never needed. Bring a PR up to date by merging main INTO the branch. Qodo anchors reviews to
  commit SHAs — a rebase forces a full re-review and loses the approved state.
- **Test execution prohibition:** AI must NEVER run tests (`pytest`, `uv run pytest`) — they need
  live clusters, providers, and credentials. Read/analyze/write/fix only.
- **No module-level provider loading (MUST, hook):** `load_source_providers()` only inside the
  pytest ecosystem (fixtures, hooks) — module-level calls run before pytest parses CLI args like
  `--providers-json` (hook: `check-no-module-load-source-providers`).
- **Deterministic tests — no defaults for our config:**

| Data source | Access pattern |
| --- | --- |
| Our config (`py_config`, `plan`, `tests_params`) | `config["key"]` — fail fast with `KeyError` |
| External/provider data | `.get("key")` + validate |
| Optional feature flags (list above) | `.get("key", False)` |

  `pytest_sessionstart` collecting required keys with `.get()` to report all missing keys in one
  `pytest.exit()` is validation-then-exit, not a silent default.

- **Provider config key access (MUST):** check `providers_schema.json` for required/optional status
  before accessing `.providers.json` fields — required: `data["key"]`; optional: `data.get("key")`;
  conditional: `.get()` + validate. Adding/renaming `.providers.json` fields updates
  `providers_schema.json` **and** `.providers.json.example` together.
- **Every OpenShift resource via `create_and_store_resource()`** (from `utilities/resources.py`):
  auto-unique names, deploy+wait, teardown tracking, 63-char truncation. Never
  `Resource(...).deploy()` directly. When intentionally deleting a resource mid-test, unregister it
  from teardown (`unregister_teardown_resource`) so session cleanup skips the missing resource.

## Fixtures

- **Autouse sparingly:** only `autouse_fixtures` in conftest.py uses `autouse=True`
  (hook: `check-autouse-fixtures`); everything else is requested explicitly.
- **Noun names** — fixtures are state, not actions (`source_provider`, not `setup_provider`).
- **Request pattern:** parameter when the test needs the value; `@pytest.mark.usefixtures()` for
  side-effect-only fixtures. Never both.
- **No unnecessary randomness:** `available_nodes[0]`, not `random.choice`.
- **Scopes:** session (`ocp_admin_client`, `session_uuid`, `target_namespace`, providers,
  `fixture_store`); class (`class_plan_config` raw config → `prepared_plan` processed config with
  cloned VMs and `source_vms_data`, used with `indirect=True` parametrization); function rarely.
  Scope changes need a comment explaining why.
- **`cleanup_migrated_vms`:** class-scoped teardown fixture, applied via
  `@pytest.mark.usefixtures` at class level; honors `--skip-teardown`.
- **`fixture_store` shape:**

```python
{
    "session_uuid": "auto-abc123",
    "base_resource_name": "auto-abc123-vsphere-8-0",
    "teardown": {"Namespace": [{"name": "ns1"}], "VirtualMachine": [{"name": "vm1"}]},
}
```

## Markers and Naming

| Marker | Purpose |
| --- | --- |
| `tier0` / `tier1` | Core (smoke) / extended functionality |
| `warm` | Warm migration |
| `remote` | Remote cluster |
| `copyoffload` (+`_sanity`, `_snapshots`) | Copy-offload (XCOPY) tests and sub-groups |
| `ca_crt` | CA certificate field (ca.crt) |
| `vsphere` | vSphere provider-specific |
| `shared_disk` | Shared disk migration |

Collection-time provider-type skipping matches tests by marker in `pytest_collection_modifyitems`
(see conftest.py for the documented pattern) — tests MUST carry the correct markers or skipping
breaks: warm → `@pytest.mark.warm`; copy-offload snapshots → `@pytest.mark.copyoffload_snapshots`;
CA cert → `@pytest.mark.ca_crt`.

Feature-gated test classes include the feature name in the class name: `Warm` for warm,
`Copyoffload` (+`Snapshot`) for copy-offload snapshots, feature name for tier1 features
(e.g. `TestLuksColdMigration`).

## Review Priority

When multiple issues exist, address in order: 1. **Correctness** (wrong behavior, data loss,
security) → 2. **Architecture** (separation of concerns, placement, responsibilities) →
3. **Determinism** (fallbacks, broad exceptions, config defaults) → 4. **Code quality** (naming,
types, duplication, dead code) → 5. **Style** (comments, logging, magic numbers).

## Parallel Execution (pytest-xdist)

Safe because: unique namespaces per session (`session_uuid`), isolated `fixture_store` per worker,
unique names from `create_and_store_resource()`. Always use fixtures for namespaces (never
hardcode); never share mutable state between tests.

## References

| Topic | Source |
| --- | --- |
| Test-plan docstring schema + accuracy checklist | [`test_plan_schema.md`](test_plan_schema.md) |
| Plan/VM configuration in depth | `docs/test-plan-configuration.md` |
| Pytest options, markers, collection | `docs/pytest-options-and-markers.md` |
| Cleanup and teardown design | `docs/cleanup-and-teardown.md` |
| Repository structure | `docs/project-structure.md` |
| Suite architecture and workflow | `docs/architecture-and-workflow.md` |
| Adding features to the suite | `docs/extending-the-suite.md` |
| `.providers.json` format | `docs/provider-config-file.md` |
| Module reference | `docs/python-module-reference.md` |
| Enforced rules (hook sources) | `scripts/hooks/` |
