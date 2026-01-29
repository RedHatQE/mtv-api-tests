# MTV API Tests - Claude Instructions

This document provides project-specific instructions for the MTV API Tests codebase.

## AI Workflow

1. **User Prompt** - User requests fix/new test/feature/enhancement
2. **Create Branch** - Create a feature branch (e.g., `feat/description` or `fix/description`)
3. **Agent Selection** - Route to appropriate specialist agent
4. **Code Changes** - Specialist implements the changes
5. **Code Review** - Delegate to `code-reviewer` agent after ANY code change
6. **Review Cycle** - Repeat steps 4-5 until no more changes needed
7. **Pre-commit** - Run `pre-commit run --all-files` and fix any failures (formatting, linting - no re-review needed)
8. **Completion** - All changes reviewed, tests pass, ready to commit

### Rules

- Run agents in PARALLEL when possible
- Never skip code-reviewer after code changes
- (MUST) Update README.md when code changes affect usage/requirements/installation/configuration
- (MUST) Update CLAUDE.md when methodology or coding patterns change. Show proposed changes to user and get approval before committing
  (These updates happen during the work, not as separate workflow steps)
- (MUST) CLAUDE.md must have NO duplications - define information once, reference elsewhere. AI context is limited.
- (MUST) CLAUDE.md must have clear, unambiguous instructions - avoid vague terms without definitions

### Commands Reference

- **Package Installation**: `uv sync`
- **Pre-commit**: `pre-commit run --all-files`
- **Container Build**: `podman build -f Dockerfile -t mtv-api-tests`

## Code Standards

- **Type Annotations (MUST):** All new functions and functions with signature changes must have complete type annotations. Use built-in Python typing (dict, list, tuple)
- **Package Management:** Use `uv` for all dependency management
- **Pre-commit (MUST):** Must pass before any commit - never use `--no-verify`
- **No Auto-Skip:** Never use `pytest.skip()` or `pytest.fail()` for validation inside fixtures or test methods.
  Validation = checking required inputs/config exist before test execution (belongs in fixtures).
  Assertions = verifying test outcomes (belongs in test methods).
  Use `@pytest.mark.skipif` at class/test level for conditional skipping.
- **Every OpenShift resource:** Must use `create_and_store_resource()` function
- **Logging Format:** Use f-strings for logging by default.
  Use parameterized format (`%s`) only for expensive operations (e.g., `large_object.to_json()`) where lazy evaluation matters.

### OpenShift/Kubernetes Resource Interactions

All cluster interactions must use `openshift-python-wrapper`. Direct `kubernetes` package usage is forbidden at runtime.

```python
# Correct imports
from ocp_resources.namespace import Namespace
from ocp_resources.secret import Secret
from ocp_resources.virtual_machine import VirtualMachine
from ocp_utilities.infra import get_client

# Forbidden imports (runtime)
from kubernetes import client  # Never
from kubernetes.dynamic import DynamicClient  # Never instantiate directly
```

**DynamicClient Rules:**

| Usage                                      | Allowed                  |
| ------------------------------------------ | ------------------------ |
| Import inside `TYPE_CHECKING` block        | Yes                      |
| String annotation `"DynamicClient"`        | Yes                      |
| Instantiate `DynamicClient(...)` directly  | No - use `get_client()`  |
| Use in `isinstance()` or runtime checks    | No                       |
| Import other `kubernetes.*` modules        | No                       |

### Function Size (SHOULD)

- **Primary:** Single responsibility - if you would write "and" in the docstring, split the function
- **Secondary:** Keep functions under 50 lines when possible. Longer functions need clear section comments
- Extract helpers with `_` prefix for sub-tasks
- Function names must clearly describe WHAT they do (e.g., `is_warm_migration_supported` not `is_supported`)

## Code Quality Rules

### Fail Fast - Validate Content Not Just Existence (MUST)

Code must never result in `None` when `None` is not valid. Fail early with clear errors.

```python
# Wrong - allows None to propagate
def get_vm_firmware(template):
    return template.spec.domain.get("firmware")

# Correct - fail fast
def get_vm_firmware(template):
    firmware = template.spec.domain.get("firmware")
    if firmware is None:
        raise ValueError(f"Firmware not found in template '{template.name}'")
    return firmware
```

**Note:** Validate content when applicable. Use `if value is None:` for None checks. Use `if not value:` only when empty containers and False are also invalid.

### Pass Objects Over Values (SHOULD)

Functions should receive objects and extract needed values internally. This improves API simplicity and maintainability.

```python
# Wrong - extracting values before passing
def create_plan(
    source_provider_name: str,
    source_provider_namespace: str,
    storage_map_name: str,
    storage_map_namespace: str,
):
    ...

create_plan(
    source_provider_name=provider.ocp_resource.name,
    source_provider_namespace=provider.ocp_resource.namespace,
    storage_map_name=storage_map.name,
    storage_map_namespace=storage_map.namespace,
)

# Correct - passing objects
def create_plan(
    source_provider: BaseProvider,
    storage_map: StorageMap,
):
    # Extract values inside the function
    name = source_provider.ocp_resource.name
    namespace = source_provider.ocp_resource.namespace
    ...

create_plan(
    source_provider=provider,
    storage_map=storage_map,
)
```

**When to apply:** When you control both the function signature and caller. Existing APIs that require extracted values may accept values.

### Variables Must Have Consistent Types (MUST)

```python
# Wrong
actual_affinity = vm.get("affinity")  # Could be dict, list, or None

# Correct
actual_affinity: dict[str, Any] = vm.get("affinity") or {}
```

### Trust Required Arguments (MUST)

Don't check if required function arguments exist.

```python
# Wrong
def compare_labels(expected_labels: dict, actual_labels: dict) -> bool:
    if expected_labels and actual_labels:
        return expected_labels == actual_labels
    return False

# Correct
def compare_labels(expected_labels: dict, actual_labels: dict) -> bool:
    return expected_labels == actual_labels
```

**Clarification:** "Trust" means don't check if required arguments were passed (they always are). "Validate at Source" means fixtures validate the VALUE they produce is valid.

### No Duplicate Code (MUST)

Extract common logic into shared functions. Create a helper when identical code appears 2+ times (copy-paste is the indicator).
Don't create abstractions for single-use or merely "similar" code.

### No Unnecessary Variables (MUST)

Avoid intermediate variables that add no clarity.

```python
# Wrong
@pytest.fixture
def my_fixture():
    result = create_resource()
    yield result

# Correct
@pytest.fixture
def my_fixture():
    yield create_resource()
```

### Exception Types (MUST)

Use specific exception types instead of generic `RuntimeError`. Create custom exceptions for domain-specific errors.

| Instead of RuntimeError | Use                                    |
| ----------------------- | -------------------------------------- |
| Invalid input/config    | `ValueError`                           |
| Missing resource        | `ValueError`                           |
| Type issues             | `TypeError`                            |
| Key not found           | `KeyError` (let propagate)             |
| Connection failures     | `ConnectionError`                      |
| Domain-specific errors  | **Custom exception class (preferred)** |

**Custom exceptions are encouraged** for domain-specific errors. They provide clearer error handling and better debugging:

```python
# Custom exceptions are encouraged for domain-specific errors
from exceptions.exceptions import MigrationTimeoutError, ProviderConnectionError

# Example custom exception usage
if not provider.is_connected():
    raise ProviderConnectionError(f"Failed to connect to provider '{provider.name}'")

if not migration.wait_for_completion(timeout=3600):
    raise MigrationTimeoutError(f"Migration '{migration.name}' timed out after 1 hour")
```

**Location:** All custom exceptions must be defined in `exceptions/exceptions.py`, not scattered in other modules. This centralizes exception definitions for better discoverability.

**Exception:** `RuntimeError` is allowed ONLY in pytest hooks for infrastructure failures (e.g., cluster unreachable, API timeout).
Use `ValueError` for configuration errors (e.g., missing config key, invalid credentials file).

### Use Empty Container Defaults (SHOULD)

Use empty containers as defaults to avoid None checks.

```python
# Wrong
firmware_spec = template.spec.domain.get("firmware")
if firmware_spec is not None:
    boot_order = firmware_spec.get("bootOrder")

# Correct
firmware_spec: dict[str, Any] = template.spec.domain.get("firmware", {})
boot_order: list = firmware_spec.get("bootOrder", [])
```

### Docstring Format (MUST)

All new functions must have docstrings with Args, Returns, and Raises sections.

```python
def process_vm(vm: VirtualMachine, options: dict[str, Any]) -> MigrationResult:
    """Process a VM for migration.

    Args:
        vm (VirtualMachine): The VM resource to process
        options (dict[str, Any]): Processing options

    Returns:
        MigrationResult: The result of the migration processing

    Raises:
        ValueError: If VM is in an invalid state
    """
```

### Validate at Source (MUST)

**Definition:** Validation = verifying required inputs, configuration values, or fixture dependencies are present
and valid before test execution. This is distinct from assertions, which verify test outcomes during execution.

Validation must happen in fixtures (where values originate), not in utility functions or test methods.

```python
# Wrong - validating in utility (too late)
def apply_node_label(labeled_worker_node, ...):
    if not labeled_worker_node:
        raise ValueError("No worker node provided")

# Wrong - validating in test method (see also: "Deterministic Tests - No Defaults for Our Config")
def test_create_storagemap(self, source_provider, ...):
    if not source_provider_data.get("storage_vendor_product"):
        pytest.fail("Missing storage_vendor_product")  # Should be in fixture

# Correct - validate in fixture
@pytest.fixture
def labeled_worker_node(worker_nodes, target_node_selector):
    node = find_node_with_selector(worker_nodes, target_node_selector)
    if not node:
        raise ValueError(f"No node found matching selector {target_node_selector}")
    return node
```

**Distinction:** Fixtures validate their own construction (is the fixture value valid?). Utility functions may validate external/provider data that varies at runtime.

Test methods should never contain validation logic - if a config value is required, create a fixture that validates it.

### Fixture Rules (MUST)

- **Autouse sparingly:** Only the `autouse_fixtures` fixture in conftest.py uses `autouse=True`. All other fixtures must be requested explicitly via parameters or `@pytest.mark.usefixtures()`
- **Noun names:** Fixtures represent resources (`source_provider` not `setup_provider`)
- **No magic skip:** Use `@pytest.mark.skipif` at test level, not `pytest.skip()` in fixtures

### Fixture Request Patterns (MUST)

- **Method parameters:** Use when you need the fixture value in the test
- **`@pytest.mark.usefixtures()`:** Use for side-effect fixtures (e.g., `cleanup_migrated_vms`) that perform setup/teardown but whose return value isn't needed by the test
- **Never list both:** Don't request via both parameter AND usefixtures - choose one

### No Unnecessary Randomness (MUST)

Tests must be deterministic. Avoid random selection when order does not matter.

```python
# Wrong
selected_node = random.choice(available_nodes)

# Correct
selected_node = available_nodes[0]
```

### Use Context Managers for Cleanup (SHOULD)

Use context managers to ensure proper resource cleanup.

```python
# Wrong
editor = ResourceEditor(node)
editor.add_label(label)
# Caller must remember to cleanup

# Correct
with ResourceEditor(node) as editor:
    editor.add_label(label)
    yield
```

## Architecture Patterns

### Provider Abstraction

- Base class: `BaseProvider` in `libs/base_provider.py`
- Implementations: VMware, RHV, OpenStack, OVA, OpenShift providers
- Context manager support for provider connections

## Critical Constraints

### Test Execution Prohibition

AI must NEVER run tests directly (`pytest`, `uv run pytest`). Tests require live clusters, provider connections, and credentials.

AI can: Read/analyze/write/fix tests, suggest improvements, review structure
AI cannot: Execute tests, validate by running

### Deterministic Tests - No Defaults for Our Config

For configurations we control (`py_config`, `plan`, `tests_params`), never use defaults:

```python
# Wrong
storage_class = py_config.get("storage_class", "default-storage")

# Correct
storage_class = py_config["storage_class"]
```

For external/provider data, `.get()` with validation is acceptable:

```python
vm_id = provider_data.get("vm_id")
if not vm_id:
    raise ValueError(f"VM ID not found for VM '{vm_name}'")
```

**Exception - Optional feature flags:**

```python
if plan.get("warm_migration", False):  # OK
    setup_warm_migration()
```

### Resource Creation - create_and_store_resource()

Every OpenShift resource must use `utilities/resources.py:create_and_store_resource()`.

```python
def create_and_store_resource(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    resource: type[Resource],
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
```

Features: auto-generates unique names, deploys and waits, stores in `fixture_store["teardown"]`, handles conflicts, truncates to 63 chars.

```python
# Correct
namespace = create_and_store_resource(
    fixture_store=fixture_store,
    resource=Namespace,
    client=ocp_admin_client,
    name="my-namespace",
)

# Wrong - bypasses tracking
namespace = Namespace(client=ocp_admin_client, name="my-namespace")
namespace.deploy()
```

## Test Structure Pattern

All tests follow a class-based structure with 5 test methods:

```python
from pytest_testconfig import config as py_config
from ocp_resources.network_map import NetworkMap
from ocp_resources.storage_map import StorageMap
from ocp_resources.migration_toolkit_virtualization import Plan

from utilities.mtv_migration import (
    create_plan_resource,
    execute_migration,
    get_network_migration_map,
    get_storage_migration_map,
)
from utilities.post_migration import check_vms


@pytest.mark.parametrize(
    "class_plan_config",
    [pytest.param(py_config["tests_params"]["test_name_here"])],
    indirect=True,
    ids=["descriptive-test-id"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
@pytest.mark.incremental
@pytest.mark.tier0  # optional: tier0/warm/remote/copyoffload
class TestNameHere:
    """Test description."""

    storage_map: StorageMap
    network_map: NetworkMap
    plan_resource: Plan

    def test_create_storagemap(self, prepared_plan, fixture_store, source_provider, destination_provider, ocp_admin_client, target_namespace, source_provider_inventory):
        """Create StorageMap resource."""
        self.__class__.storage_map = get_storage_migration_map(...)
        assert self.storage_map

    def test_create_networkmap(self, prepared_plan, fixture_store, source_provider, destination_provider, ocp_admin_client, target_namespace, source_provider_inventory, multus_network_name):
        """Create NetworkMap resource."""
        self.__class__.network_map = get_network_migration_map(
            multus_network_name=multus_network_name, ...
        )
        assert self.network_map

    def test_create_plan(self, prepared_plan, fixture_store, source_provider, destination_provider, ocp_admin_client, target_namespace):
        """Create MTV Plan CR resource."""
        self.__class__.plan_resource = create_plan_resource(
            storage_map=self.storage_map,
            network_map=self.network_map,
            ...
        )
        assert self.plan_resource

    def test_migrate_vms(self, fixture_store, ocp_admin_client, target_namespace):
        """Execute migration."""
        execute_migration(
            plan=self.plan_resource,
            ...
        )

    def test_check_vms(self, prepared_plan, source_provider, destination_provider, target_namespace, source_provider_data, source_vms_namespace, source_provider_inventory):
        """Validate migrated VMs."""
        check_vms(
            plan=prepared_plan,
            network_map_resource=self.network_map,
            storage_map_resource=self.storage_map,
            ...
        )
```

### Key Patterns

- **Class-level parametrization**: Use `class_plan_config` with `indirect=True`
- **Shared state**: Store resources on class with `self.__class__.attribute`
- **Test ordering**: Use `@pytest.mark.incremental` at class level for sequential test dependencies
- **5-step pattern**: storagemap -> networkmap -> plan -> migrate -> check_vms

**Test method naming:** Test methods do one step each: `test_create_storagemap`, `test_create_networkmap`, `test_create_plan`, `test_migrate_vms`, `test_check_vms`

**Fixture parameters:** Each test method requests only the fixtures it needs. The example shows typical patterns.

### Adding New Tests

1. Add configuration to `tests/tests_config/config.py`:

```python
tests_params: dict = {
    "test_my_new_test": {
        "virtual_machines": [{"name": "vm-name", "source_vm_power": "on", "guest_agent": True}],
        "warm_migration": False,
    },
}
```

1. Create test file `tests/test_<feature>_migration.py`
2. Create a test class with `@pytest.mark.parametrize` using `class_plan_config` and `indirect=True`
3. Add pytest markers at class level (tier0, warm, remote, copyoffload)
4. Implement the 5 test methods following the pattern in the example above

**VM Configuration Options:**

| Option            | Required | Values                              |
| ----------------- | -------- | ----------------------------------- |
| `name`            | Yes      | VM name in source provider          |
| `source_vm_power` | No       | "on" or "off"                       |
| `guest_agent`     | No       | True if installed                   |
| `clone`           | No       | True to clone before migration      |
| `disk_type`       | No       | "thin", "thick-lazy", "thick-eager" |

## Fixture Patterns

### conftest.py Structure

Only pytest fixtures and hooks belong in conftest.py. Helper functions go to `utilities/`.

### Fixture Scopes

**Session-scoped** - shared across all tests:

```python
@pytest.fixture(scope="session")
def ocp_admin_client():
    return get_cluster_client()
```

Common: `ocp_admin_client`, `session_uuid`, `target_namespace`, `source_provider`, `destination_provider`, `fixture_store`

**Class-scoped** - per test class:

Two class-scoped fixtures work together (used with `indirect=True` parametrization):

- `class_plan_config`: Raw test configuration from `@pytest.mark.parametrize`
- `prepared_plan`: Processed config with cloned VMs, updated names, and `source_vms_data`

Test methods receive `prepared_plan` which is ready to use:

```python
@pytest.fixture(scope="class")
def prepared_plan(class_plan_config, fixture_store, source_provider, ...):
    plan: dict[str, Any] = deepcopy(class_plan_config)
    # Clone VMs, update names
    plan["source_vms_data"] = {}  # Separate storage for source VM data
    yield plan
    # Track for cleanup
```

**Function-scoped** (default) - per test method:

Function-scoped fixtures are rarely needed in this codebase. Most fixtures are session or class scoped. If you need per-test isolation, use function scope but this is uncommon.

### cleanup_migrated_vms Fixture

Class-scoped teardown fixture that cleans up migrated VMs after each test class completes.

**Usage:** Add via `@pytest.mark.usefixtures("cleanup_migrated_vms")` at class level.

**Behavior:**

- Runs after all tests in the class complete (teardown-only fixture)
- Uses `vm_obj.clean_up()` from ocp_resources for proper VM cleanup
- Honors `--skip-teardown` flag (skips cleanup when flag is set)
- Session-level teardown catches any leftover VMs not cleaned by class fixtures

### fixture_store Structure

```python
{
    "session_uuid": "auto-abc123",
    "base_resource_name": "auto-abc123-vsphere-8-0",
    "teardown": {
        "Namespace": [{"name": "ns1", ...}],
        "VirtualMachine": [{"name": "vm1", ...}],
    },
}
```

## Test Markers

| Marker        | Purpose                          |
| ------------- | -------------------------------- |
| `tier0`       | Core functionality (smoke tests) |
| `warm`        | Warm migration tests             |
| `remote`      | Remote cluster tests             |
| `copyoffload` | Copy-offload (XCOPY) tests       |

```python
@pytest.mark.tier0
@pytest.mark.warm
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote cluster")
class TestRemoteWarmMigration:
    ...
```

## Parallel Execution (pytest-xdist)

Tests are parallel-safe because:

- Unique namespaces per session via `session_uuid`
- Each worker has isolated `fixture_store`
- `create_and_store_resource()` generates unique names

Rules:

- Always use fixtures for namespaces (never hardcode)
- Never share mutable state between tests
