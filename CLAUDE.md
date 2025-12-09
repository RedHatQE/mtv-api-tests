# MTV API Tests - Claude Instructions

## AI Workflow (MANDATORY)

**This is the required workflow for all code changes to this repository:**

1. **User Prompt** → User requests fix/new test/feature/enhancement
2. **Agent Selection** - AI analyzes request and triggers appropriate specialist agent:
   - Python code - `python-expert`
   - Git operations - `git-expert`
   - Documentation - `technical-documentation-writer`
   - Other specialists as needed
3. **Code Changes** - Specialist agent implements the changes
4. **Code Review (ALWAYS)** - After ANY code change, AUTOMATICALLY trigger `code-reviewer`
5. **Review Cycle** - If code-reviewer makes additional changes:
   - Return to step 4 (trigger code-reviewer again)
   - Repeat until no more changes are needed
6. **Completion** - All changes reviewed and approved

**CRITICAL RULES:**

- **MANDATORY:** ALL code changes MUST use specialist agents - NEVER modify code directly
- **MANDATORY:** Python code - `python-expert` agent
- **MANDATORY:** Git operations - `git-expert` agent
- **MANDATORY:** Documentation - `technical-documentation-writer` agent
- **MANDATORY:** NEVER work on main branch - ALWAYS create a feature branch first
- **MANDATORY:** Update README.md when code changes affect usage, requirements, installation, or configuration
- **MANDATORY:** Run agents in PARALLEL when possible - launch all independent agents in a SINGLE message
- **MANDATORY:** After ANY code change - `code-reviewer` agent (AUTOMATIC)
- **NEVER** skip code-reviewer after code changes
- **ALWAYS** repeat the review cycle if code-reviewer makes changes
- **PROHIBITED:** Direct use of Edit/Write tools for code files - agents ONLY

## CRITICAL: Agent Usage is MANDATORY

**ALL code operations MUST be performed through specialist agents. Direct code modification is STRICTLY
PROHIBITED.**

### When MUST You Use Agents

| Operation                    | Agent Required                   | Example                                   |
| ---------------------------- | -------------------------------- | ----------------------------------------- |
| Python code (any .py file)   | `python-expert`                  | Write test, fix bug, add utility function |
| Git operations               | `git-expert`                     | Commit, branch, push, create PR           |
| Documentation (README, docs) | `technical-documentation-writer` | Update docs, write guides                 |
| Code review                  | `code-reviewer`                  | Review after ANY code change              |

### What is FORBIDDEN

- ❌ **NEVER** use `Edit` tool on `.py` files directly
- ❌ **NEVER** use `Write` tool to create `.py` files directly
- ❌ **NEVER** modify code without routing to appropriate agent
- ❌ **NEVER** skip the agent → code-reviewer workflow

### What is ALLOWED

- ✅ Use `Read` tool to read any file
- ✅ Use `Grep` to search code
- ✅ Use `Glob` to find files
- ✅ Use agents for ALL code modifications

### Enforcement

If you modify code directly instead of using an agent:

1. **VIOLATION** - Stop immediately
2. Undo the changes
3. Route to the correct agent
4. Follow the proper workflow

**Remember:** Analysis and reading = OK. Code changes = AGENT REQUIRED.

## MTV API Tests Specific Guidelines

### Code Quality Requirements

- **Type Annotations:** Always use built-in Python typing (dict, list, tuple, etc.)
- **Package Management:** Use `uv` for all dependency and project management
- **OpenShift Integration:** Always use `openshift-python-wrapper` for cluster interactions
- **Pre-commit:** Must pass before any commit - never use `git commit --no-verify`
- **Code Simplicity:** Keep code simple and readable, avoid over-engineering
- Every openshift resource must be created using `create_and_store_resource` function only.

### Testing Standards

- **Real Environments:** Tests run against real clusters and real providers
- **Provider Support:** VMware vSphere, RHV, OpenStack, OVA, OpenShift
- **Migration Types:** Both cold and warm migration testing
- **Resource Management:** Comprehensive fixture-based cleanup and teardown
- **Parallel Execution:** Support for pytest-xdist concurrent testing

### Development Workflow

- **Package Installation:** `uv sync`
- **Test Execution:** `uv run pytest <options>` (on live clusters only - AI cannot run tests)
- **Linting:** `ruff check` and `ruff format`
- **Type Checking:** `mypy` with strict configuration
- **Container Build:** `podman build -f Dockerfile -t mtv-api-tests`

### Configuration Management

- **Provider Configuration:** `.providers.json` file in project root
- **Test Configuration:** pytest-testconfig for test parameters
- **Environment Variables:** OpenShift cluster and provider authentication
- **Storage Classes:** Configurable via test configuration (nfs, csi, etc.)

## Architecture Patterns

### Provider Abstraction

- **Base Class:** `BaseProvider` in `libs/base_provider.py`
- **Provider Implementations:** VMware, RHV, OpenStack, OVA, OpenShift providers
- **Unified VM Representation:** Consistent VM data structure across providers
- **Connection Management:** Context manager support for provider connections

### Test Structure

- **Fixtures:** Session-scoped fixtures for resource management in `conftest.py`
- **Parametrization:** Provider-specific test parametrization
- **Markers:** tier0, tier1, scale markers for test categorization
- **Data Collection:** Automatic log and must-gather collection on failures

### Resource Management

- **Cleanup Strategy:** Comprehensive teardown with fixture tracking
- **Namespace Management:** Unique namespace generation per test session
- **Resource Tracking:** JSON-based resource tracking for post-test cleanup
- **Error Recovery:** Must-gather data collection on test failures

## Critical Constraints and Patterns

### CRITICAL: Test Execution Prohibition

**AI must NEVER run tests directly.**

- ❌ **FORBIDDEN:** `pytest`, `uv run pytest`, any test execution commands
- ✅ **ALLOWED:** Analyze tests, write tests, fix tests, read test output

**Why:** Tests require:

- Live OpenShift cluster with MTV operator installed
- Real provider connections (VMware vSphere, RHV, OpenStack, etc.)
- Network access to source infrastructure
- Configured credentials and authentication

**AI can:**

- Read and analyze test code
- Write new tests following patterns (via `python-expert` agent)
- Fix failing tests based on logs (via `python-expert` agent)
- Suggest test improvements
- Review test structure (via `code-reviewer` agent)

**AI cannot:**

- Execute pytest commands
- Run individual tests
- Validate tests by running them
- Modify code directly (must use agents)

### CRITICAL: Deterministic Tests - No Defaults/Fallbacks

**All code must be deterministic with NO assumptions or default values for configurations WE CONTROL.**

**This rule applies to configurations defined in our codebase:**

- `py_config` - test configuration we define in `tests/tests_config/config.py`
- `plan` - test plan parameters we define in test parametrization
- `tests_params` - test parameters dictionary we define
- Any configuration dictionary or structure defined in our codebase

**For OUR configurations - NEVER use defaults:**

```python
# ❌ WRONG - Uses fallback/default for OUR configuration
storage_class = py_config.get("storage_class", "default-storage")
vm_name = plan.get("vm_name", "default-vm")
timeout = py_config.get("plan_wait_timeout", 3600)

# ✅ CORRECT - Deterministic, fails fast with KeyError
storage_class = py_config["storage_class"]
vm_name = plan["virtual_machines"][0]["name"]
timeout = py_config["plan_wait_timeout"]
```

**For external/provider configurations - `.get()` with validation is acceptable:**

```python
# ✅ ACCEPTABLE - External config with explicit validation and meaningful error
vm_id = provider_data.get("vm_id")
if not vm_id:
    raise ValueError(f"VM ID not found in provider data for VM '{vm_name}'.")

# ✅ ACCEPTABLE - Provider API response with validation
disk_type = vm_details.get("disk_type")
if disk_type not in ["thin", "thick-lazy", "thick-eager"]:
    raise ValueError(f"Unsupported disk type '{disk_type}' for VM '{vm_name}'")
```

**Why this distinction:**

- **Our config:** We control it, should be complete, fail fast if missing (KeyError)
- **External data:** We don't control it, explicit validation provides better error messages than raw KeyError
- Tests must fail immediately if OUR configuration is incomplete
- External data validation should provide context-rich error messages

**Exception - Optional feature flags in OUR config:**

```python
# ✅ OK - Optional boolean feature flag in our config
if plan.get("warm_migration", False):  # Optional feature, defaults to False
    setup_warm_migration()

# ✅ OK - Optional list in our config
hooks = plan.get("pre_migration_hooks", [])  # Optional hooks, defaults to empty
for hook in hooks:
    execute_hook(hook)
```

### Resource Creation Pattern - MANDATORY

**CRITICAL: Every OpenShift resource MUST be created using `create_and_store_resource()` function.**

**Location:** `utilities/resources.py:create_and_store_resource()`

**Why This Pattern:**

- Automatic resource tracking for cleanup
- Unique name generation with UUID (prevents conflicts in parallel execution)
- Handles resource conflicts gracefully (reuses existing)
- Ensures cleanup even when tests fail or crash
- Truncates names to Kubernetes 63-character limit
- Thread-safe for parallel test execution (pytest-xdist)

**Function Signature:**

```python
def create_and_store_resource(
    client: DynamicClient,
    fixture_store: dict[str, Any],
    resource: type[Resource],
    test_name: str | None = None,
    **kwargs: Any,
) -> Any:
```

**Correct Usage Examples:**

```python
# Namespace creation
namespace = create_and_store_resource(
    fixture_store=fixture_store,
    resource=Namespace,
    client=ocp_admin_client,
    name="my-namespace",
    label={"app": "mtv-tests"},
)

# Provider creation
provider = create_and_store_resource(
    fixture_store=fixture_store,
    resource=Provider,
    client=ocp_admin_client,
    namespace=target_namespace,
    secret_name=secret.name,
    url=provider_url,
    provider_type=Provider.ProviderType.VSPHERE,
)

# Secret creation
secret = create_and_store_resource(
    client=ocp_admin_client,
    fixture_store=fixture_store,
    resource=Secret,
    namespace=target_namespace,
    string_data={
        "user": username,
        "password": password,
        "insecureSkipVerify": "true",
    },
)

# NetworkAttachmentDefinition
nad = create_and_store_resource(
    fixture_store=fixture_store,
    resource=NetworkAttachmentDefinition,
    client=ocp_admin_client,
    namespace=target_namespace,
    name="bridge-network",
    cni_type="bridge",
    config=multus_config,
)
```

**What NEVER to do:**

```python
# ❌ WRONG - Direct resource instantiation bypasses tracking
namespace = Namespace(client=ocp_admin_client, name="my-namespace")
namespace.deploy()

# ❌ WRONG - No cleanup tracking
provider = Provider(
    client=ocp_admin_client,
    namespace=target_namespace,
    secret_name=secret.name,
)
provider.deploy(wait=True)

# ❌ WRONG - Manual resource creation
secret_dict = {...}
Secret(**secret_dict).deploy()
```

**Key Features:**

1. **Auto-generates unique names** using session UUID if name not provided
2. **Deploys resource** and waits for readiness by default
3. **Stores metadata** in `fixture_store["teardown"]` for cleanup
4. **Handles conflicts** - reuses existing resources if already created
5. **Name truncation** - ensures names don't exceed 63 chars (Kubernetes limit)
6. **Test tracking** - optional `test_name` for test-specific resources

### Test Structure Pattern - How to Add New Tests

**ALL tests in this repository follow the same structure.**

**Standard Test Pattern:**

```python
from pytest_testconfig import config as py_config
from utilities.mtv_migration import create_storagemap_and_networkmap, migrate_vms

@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_name_here"])],
    indirect=True,  # REQUIRED - passes to plan fixture first
    ids=["descriptive-test-id"],
)
@pytest.mark.tier0  # or tier1, tier2
@pytest.mark.warm   # optional: warm/remote/copyoffload/scale/negative
def test_name_here(
    request,                    # pytest request object
    fixture_store,              # resource tracking dictionary
    ocp_admin_client,           # OpenShift DynamicClient
    target_namespace,           # test namespace
    destination_provider,       # destination provider
    plan,                       # test plan from parametrize
    source_provider,            # source provider connection
    source_provider_data,       # source provider config
    multus_network_name,        # multus network name
    source_provider_inventory,  # provider inventory
    source_vms_namespace,       # source VMs namespace
):
    # 1. Create storage and network migration maps
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plan,
    )

    # 2. Execute migration
    migrate_vms(
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
        source_provider_inventory=source_provider_inventory,
    )
```

**Step-by-Step Guide to Add New Test:**

1. **Add test configuration** to `tests/tests_config/config.py`:

   ```python
   tests_params: dict = {
       "test_my_new_test": {
           "virtual_machines": [
               {
                   "name": "vm-name-in-source",
                   "source_vm_power": "on",  # or "off"
                   "guest_agent": True,      # if guest agent installed
               },
           ],
           "warm_migration": False,  # True for warm, False for cold
       },
   }
   ```

2. **Create test file** in `tests/` directory:
   - File naming: `test_<feature>_migration.py`
   - Example: `test_my_feature_migration.py`

3. **Import required modules** (see example above)

4. **Add parametrize decorator** with your test config:

   ```python
   @pytest.mark.parametrize(
       "plan",
       [pytest.param(py_config["tests_params"]["test_my_new_test"])],
       indirect=True,
       ids=["my-test-id"],
   )
   ```

5. **Add pytest markers**:
   <!-- - **Required:** `@pytest.mark.tier0` (or tier1, tier2) -->
   - **Optional:** `@pytest.mark.warm`, `@pytest.mark.remote`, `@pytest.mark.copyoffload`

6. **Define test function** with standard fixtures (see example above)

7. **Follow the two-step pattern:**
   - Step 1: Create migration maps
   - Step 2: Execute migration

### Example: Adding a new warm migration test

File: `tests/test_my_warm_migration.py`

```python
import pytest
from pytest_testconfig import config as py_config

from utilities.mtv_migration import create_storagemap_and_networkmap, migrate_vms


@pytest.mark.parametrize(
    "plan",
    [pytest.param(py_config["tests_params"]["test_my_warm_migration"])],
    indirect=True,
    ids=["warm-rhel9"],
)
@pytest.mark.tier1
@pytest.mark.warm
def test_my_warm_migration(
    request,
    fixture_store,
    ocp_admin_client,
    target_namespace,
    destination_provider,
    plan,
    source_provider,
    source_provider_data,
    multus_network_name,
    source_provider_inventory,
    source_vms_namespace,
):
    storage_migration_map, network_migration_map = create_storagemap_and_networkmap(
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        source_provider_inventory=source_provider_inventory,
        ocp_admin_client=ocp_admin_client,
        multus_network_name=multus_network_name,
        target_namespace=target_namespace,
        plan=plan,
    )

    migrate_vms(
        ocp_admin_client=ocp_admin_client,
        request=request,
        fixture_store=fixture_store,
        source_provider=source_provider,
        destination_provider=destination_provider,
        plan=plan,
        network_migration_map=network_migration_map,
        storage_migration_map=storage_migration_map,
        source_provider_data=source_provider_data,
        target_namespace=target_namespace,
        source_vms_namespace=source_vms_namespace,
        source_provider_inventory=source_provider_inventory,
    )
```

**DO NOT:**

- ❌ Create custom test structures
- ❌ Skip parametrization
- ❌ Hardcode VM names in test (use config)
- ❌ Create resources without `create_and_store_resource()`
- ❌ Skip markers (tier0/tier1/tier2 required)

### Test Configuration Pattern

**All test configurations are centralized in `tests/tests_config/config.py`.**

**Configuration Structure:**

```python
global config

# Global settings (module-level variables)
insecure_verify_skip: str = "true"
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "mtv-api-tests"
mtv_namespace: str = "openshift-mtv"
plan_wait_timeout: int = 3600
remote_ocp_cluster: str = ""

# Test-specific parameters
tests_params: dict = {
    "test_name": {
        "virtual_machines": [
            {
                "name": "vm-name",
                "source_vm_power": "on",  # "on" or "off"
                "guest_agent": True,      # True if GA installed
            },
        ],
        "warm_migration": False,  # True for warm, False for cold
    },
}

# Auto-export to pytest config (DO NOT MODIFY THIS SECTION)
for _dir in dir():
    val = locals()[_dir]
    if type(val) not in [bool, list, dict, str, int]:
        continue
    if _dir in ["encoding", "py_file", "__annotations__"]:
        continue
    config[_dir] = locals()[_dir]
```

**How to Add New Test Configuration:**

1. **Add entry to `tests_params` dictionary**:

   ```python
   tests_params: dict = {
       # Existing tests...

       # Your new test
       "test_my_new_feature": {
           "virtual_machines": [
               {
                   "name": "source-vm-name",
                   "source_vm_power": "on",
                   "guest_agent": True,
               },
           ],
           "warm_migration": False,
       },
   }
   ```

2. **VM configuration options:**

   ```python
   {
       "name": "vm-name",              # REQUIRED: VM name in source provider
       "source_vm_power": "on",         # Optional: "on" or "off"
       "guest_agent": True,             # Optional: True if guest agent installed
       "clone": True,                   # Optional: Clone VM before migration
       "disk_type": "thin",             # Optional: "thin", "thick-lazy", "thick-eager"
   }
   ```

3. **Access in tests:**

   ```python
   from pytest_testconfig import config as py_config

   # Access test parameters
   plan_config = py_config["tests_params"]["test_my_new_feature"]

   # Use in parametrize
   @pytest.mark.parametrize(
       "plan",
       [pytest.param(py_config["tests_params"]["test_my_new_feature"])],
       indirect=True,
   )
   ```

4. **Access global settings:**

   ```python
   # Global configuration
   namespace_prefix = py_config["target_namespace_prefix"]
   plan_timeout = py_config["plan_wait_timeout"]
   mtv_namespace = py_config["mtv_namespace"]
   ```

**Configuration Requirements (MANDATORY):**

- ✅ **MUST** use descriptive test parameter names matching test function names
- ✅ **MUST** specify required VM properties (name is required)
- ✅ **MUST** set `warm_migration` explicitly (True/False, no defaults)
- ✅ **MUST** use type annotations for global settings
- ❌ **NEVER** modify the auto-export loop at the bottom
- ❌ **NEVER** add non-serializable types to config

### Fixture Patterns

**This project uses pytest fixtures extensively for resource management.**

#### Session-Scoped Fixtures

**Used for resources shared across ALL tests in a session:**

```python
@pytest.fixture(scope="session")
def ocp_admin_client():
    """Single OpenShift client for entire test session"""
    return get_cluster_client()

@pytest.fixture(scope="session")
def session_uuid(fixture_store):
    """Unique identifier for this test session"""
    _uuid = generate_name_with_uuid(name="mtv-api-tests")
    fixture_store["session_uuid"] = _uuid
    return _uuid

@pytest.fixture(scope="session")
def source_provider(fixture_store, target_namespace, ...):
    """Single provider connection for entire session"""
    with create_source_provider(
        source_provider_data=source_provider_data,
        namespace=target_namespace,
        admin_client=ocp_admin_client,
        session_uuid=fixture_store["session_uuid"],
        fixture_store=fixture_store,
    ) as _source_provider:
        yield _source_provider
    _source_provider.disconnect()
```

**Common session fixtures:**

- `ocp_admin_client` - OpenShift DynamicClient
- `session_uuid` - Unique session identifier
- `target_namespace` - Test namespace
- `source_provider` - Source provider connection
- `destination_provider` - Destination provider connection
- `fixture_store` - Resource tracking dictionary

#### Function-Scoped Fixtures

**Used for resources created per test:**

```python
@pytest.fixture(scope="function")
def plan(request, fixture_store, source_provider, ...):
    """New migration plan for each test"""
    # Get plan from parametrize
    plan: dict[str, Any] = request.param
    virtual_machines: list[dict[str, Any]] = plan["virtual_machines"]

    # Clone VMs for testing (don't modify source)
    for vm in virtual_machines:
        source_vm_details = source_provider.vm_dict(
            name=vm["name"],
            clone=True,
            session_uuid=fixture_store["session_uuid"],
        )
        vm["name"] = source_vm_details["name"]  # Update with cloned name

    yield plan

    # Teardown: track resources for cleanup
    for vm in plan["virtual_machines"]:
        fixture_store["teardown"].setdefault(VirtualMachine.kind, []).append({
            "name": vm["name"],
            "namespace": target_namespace,
        })
```

#### fixture_store Pattern

**Used for cross-fixture communication and resource tracking:**

```python
# Store data for other fixtures to access
@pytest.fixture(scope="session")
def base_resource_name(fixture_store, session_uuid, source_provider_data):
    name = f"{session_uuid}-{source_provider_data['type']}"
    fixture_store["base_resource_name"] = name  # Store for later use
    return name

# Access stored data from another fixture
@pytest.fixture(scope="function")
def my_resource(fixture_store, ocp_admin_client):
    # Access base_resource_name from fixture_store
    base_name = fixture_store["base_resource_name"]

    resource = create_and_store_resource(
        fixture_store=fixture_store,
        resource=MyResource,
        client=ocp_admin_client,
        name=f"{base_name}-my-resource",
    )
    return resource
```

**fixture_store Structure:**

```python
{
    "session_uuid": "mtv-api-tests-abc123",
    "base_resource_name": "mtv-api-tests-abc123-vsphere-8-0",
    "vms_for_current_session": {"vm1", "vm2", "vm3"},
    "teardown": {
        "Namespace": [{"name": "ns1", ...}],
        "Provider": [{"name": "provider1", ...}],
        "VirtualMachine": [{"name": "vm1", ...}],
    },
}
```

**Fixture Requirements (MANDATORY):**

- ✅ **MUST** use session scope for shared, expensive resources (providers, clients)
- ✅ **MUST** use function scope for test-specific resources (plans, VMs)
- ✅ **MUST** store cross-fixture data in `fixture_store`
- ✅ **MUST** track ALL resources in `fixture_store["teardown"]` for cleanup
- ❌ **NEVER** create resources without `create_and_store_resource()`
- ❌ **NEVER** modify shared session fixtures in tests

### Test Markers and Categorization

**Pytest markers are used to categorize and selectively run tests.**

**Available Markers (defined in `pytest.ini`):**

| Marker          | Purpose                          | Example                         |
| --------------- | -------------------------------- | ------------------------------- |
| `tier0`         | Core functionality (smoke tests) | Basic cold migration            |
| `tier1`         | Extended functionality           | Advanced features               |
| `tier2`         | Additional scenarios             | Edge cases                      |
| `warm`          | Warm migration tests             | Incremental snapshot migrations |
| `remote`        | Remote cluster tests             | Cross-cluster migrations        |
| `copyoffload`   | Copy-offload (XCOPY) tests       | VMware copyoffload              |
| `scale`         | Scale/performance tests          | Multiple VMs                    |
| `negative`      | Negative test cases              | Error conditions                |
| `customer_case` | Customer-reported issues         | Bug reproductions               |

**How to Apply Markers:**

```python
# Single marker
@pytest.mark.tier0
def test_basic_migration(...):
    pass

# Multiple markers
@pytest.mark.tier1
@pytest.mark.warm
def test_warm_migration(...):
    pass

# Conditional skip
@pytest.mark.remote
@pytest.mark.skipif(
    not get_value_from_py_config("remote_ocp_cluster"),
    reason="No remote OCP cluster provided"
)
def test_remote_migration(...):
    pass

# Customer case
@pytest.mark.customer_case
@pytest.mark.tier0
def test_customer_issue_123(...):
    pass
```

**Marker Requirements:**

- ✅ **REQUIRED:** Every test MUST have a tier marker (tier0, tier1, or tier2)
- ✅ **OPTIONAL:** Add feature markers (warm, remote, copyoffload, scale, negative)
- ✅ **DESCRIPTIVE:** Use customer_case for bug reproductions
- ❌ **DON'T:** Create tests without tier markers
- ❌ **DON'T:** Use undefined markers (must be in pytest.ini)

### Parallel Execution Support

**Tests support parallel execution using pytest-xdist.**

**Why Parallel Execution is Safe:**

1. **Unique namespaces per session:**
   - Each test session gets a unique UUID via `session_uuid` fixture
   - Namespace pattern: `{target_namespace_prefix}-{session_uuid}`
   - Prevents resource conflicts between parallel runs

2. **Resource isolation:**
   - Each worker has its own `fixture_store`
   - Resources tracked independently per worker
   - Cleanup happens per worker

3. **Thread-safe patterns:**
   - ✅ Unique resource names (UUID-based via `create_and_store_resource()`)
   - ✅ Isolated namespaces per worker
   - ✅ Independent fixture stores
   - ✅ No shared mutable state between tests

**Key Takeaway for Code:**

- Always use fixtures for namespaces (never hardcode)
- Always use `create_and_store_resource()` for unique names
- Never share mutable state between tests
- Session fixtures create expensive resources once, reused across tests in same worker
