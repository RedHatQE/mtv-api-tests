# Remote OpenShift Migrations

Remote OpenShift migrations in `mtv-api-tests` use the same MTV lifecycle as the default OpenShift destination flow. The suite still creates a `StorageMap`, creates a
`NetworkMap`, creates a `Plan`, executes the migration, and validates the migrated VMs.

If you already know the default local-destination flow, the easiest way to understand the remote path is this: the test flow stays the same, but the destination OpenShift
provider changes from the implicit local form to an explicit provider that includes an API URL and token-backed secret.

## What "remote" means in this repository

The repository exposes remote scenarios as dedicated pytest classes marked with `remote`. There are four of them:

| Class | Plan config | `ids` |
| --- | --- | --- |
| `TestColdRemoteOcp` | `test_cold_remote_ocp` | `MTV-79` |
| `TestWarmRemoteOcp` | `test_warm_remote_ocp` | `MTV-394` |
| `TestClusterroleWarmMtvMigration` | `test_mtv_clusterrole_warm_migration` | `MTV-3129-clusterrole-warm` |
| `TestClusterroleWarmWithSccMigration` | `test_mtv_clusterrole_warm_migration_with_scc` | `MTV-3129-...-with-scc` |

`TestColdRemoteOcp` lives in `tests/cold/test_mtv_cold_migration.py`. The other three live in `tests/warm/`.

All of them are skipped unless `remote_ocp_cluster` is set:

```463:481:tests/warm/test_mtv_warm_migration.py
@pytest.mark.vsphere
@pytest.mark.rhv
@pytest.mark.warm
@pytest.mark.remote
@pytest.mark.incremental
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_warm_remote_ocp"],
        )
    ],
    indirect=True,
    ids=["MTV-394"],
)
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
@pytest.mark.usefixtures("precopy_interval_forkliftcontroller", "cleanup_migrated_vms")
class TestWarmRemoteOcp:
    """Warm remote OCP migration test."""
```

The cold remote class is gated the same way and additionally uses `destination_ocp_provider` instead of `destination_provider`:

```164:179:tests/cold/test_mtv_cold_migration.py
@pytest.mark.remote
@pytest.mark.incremental
@pytest.mark.skipif(not get_value_from_py_config("remote_ocp_cluster"), reason="No remote OCP cluster provided")
@pytest.mark.parametrize(
    "class_plan_config",
    [
        pytest.param(
            py_config["tests_params"]["test_cold_remote_ocp"],
        )
    ],
    indirect=True,
    ids=["MTV-79"],
)
@pytest.mark.usefixtures("cleanup_migrated_vms")
class TestColdRemoteOcp:
    """Cold migration test to remote OCP cluster."""
```

The repository's `pytest.ini` registers the `remote` marker and loads default config from `tests/tests_config/config.py`.

> **Note:** In this codebase, remote scenarios do not introduce a second checked-in cluster credential set. The active OpenShift connection still comes from the standard cluster
> settings.

## Destination Provider Handling

There are three destination provider fixtures in this repository. Two of them are used by remote scenarios.

### Default local destination flow

The default destination fixture in `conftest.py` creates an OpenShift `Provider` with an empty `url` and an empty `secret` block. That is the local, in-cluster destination path
used by the non-remote tests.

```688:704:conftest.py
@pytest.fixture(scope="session")
def destination_provider(session_uuid, ocp_admin_client, target_namespace, fixture_store):
    kind_dict = {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "Provider",
        "metadata": {"name": f"{session_uuid}-local-ocp-provider", "namespace": target_namespace},
        "spec": {"secret": {}, "type": "openshift", "url": ""},
    }

    provider = create_and_store_resource(
        fixture_store=fixture_store,
        resource=Provider,
        kind_dict=kind_dict,
        client=ocp_admin_client,
    )

    return OCPProvider(ocp_resource=provider, fixture_store=fixture_store)
```

### Remote destination flow

The remote path creates a secret from the active OpenShift API token and then creates an explicit OpenShift `Provider` with a real API URL.

```963:993:conftest.py
@pytest.fixture(scope="session")
def destination_ocp_secret(fixture_store, ocp_admin_client, target_namespace):
    api_key: str = ocp_admin_client.configuration.api_key.get("authorization")
    if not api_key:
        raise ValueError("API key not found in configuration")

    secret = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Secret,
        namespace=target_namespace,
        # API key format: 'Bearer sha256~<token>', split it to get token.
        string_data={"token": api_key.split()[-1], "insecureSkipVerify": "true"},
    )
    yield secret


@pytest.fixture(scope="session")
def destination_ocp_provider(fixture_store, destination_ocp_secret, ocp_admin_client, session_uuid, target_namespace):
    provider = create_and_store_resource(
        client=ocp_admin_client,
        fixture_store=fixture_store,
        resource=Provider,
        name=f"{session_uuid}-destination-ocp-provider",
        namespace=target_namespace,
        secret_name=destination_ocp_secret.name,
        secret_namespace=destination_ocp_secret.namespace,
        url=ocp_admin_client.configuration.host,
        provider_type=Provider.ProviderType.OPENSHIFT,
    )
    yield OCPProvider(ocp_resource=provider, fixture_store=fixture_store)
```

This fixture backs `TestWarmRemoteOcp` and `TestColdRemoteOcp`.

### ClusterRole destination flow

The two `TestClusterrole*` classes use a different destination provider defined in `tests/warm/conftest.py`. Instead of reusing the session API token, it creates a fresh
`ServiceAccount` in the MTV namespace, binds it to the pre-existing `forklift-migrator-role` ClusterRole, waits for the ServiceAccount token Secret to be populated, and uses that
token as the Provider secret:

```56:59:tests/warm/conftest.py
sa_name = f"{session_uuid}-forklift-migrator-sa"
binding_name = f"{session_uuid}-forklift-migrator-binding"
token_secret_name = f"{session_uuid}-clusterrole-token"
provider_name = f"{session_uuid}-clusterrole-destination-ocp-provider"
```

```38:41:tests/warm/conftest.py
3. Create a token for that SA (equivalent to: oc create token <sa> -n <mtv-namespace>)
4. Create Forklift Provider CR using that token (in target namespace)

Does NOT create the ClusterRole; forklift-migrator-role must already exist in the cluster.
```

`TestClusterroleWarmMtvMigration` runs a warm migration that is expected to fail without an SCC binding, and `TestClusterroleWarmWithSccMigration` adds a class-scoped
`forklift_scc_binding` fixture that grants `forklift-controller-scc` to the default ServiceAccount, so the same migration is expected to succeed.

For users, the practical difference is straightforward:

- Local destination tests use the implicit local OpenShift provider form.
- Remote destination tests use an explicit OpenShift provider with a token and API URL.
- The ClusterRole remote tests use an explicit OpenShift provider backed by a dedicated ServiceAccount token.
- The destination provider is created automatically at runtime. You do not define it in `.providers.json`.

> **Note:** The destination secret hardcodes `insecureSkipVerify: "true"` for the remote OpenShift provider. There is no separate destination-side SSL verification toggle in
> `tests/tests_config/config.py`.

## Remote-Specific Configuration

The shared config file already contains the remote toggle and the runtime knobs that matter to remote runs:

```5:15:tests/tests_config/config.py
insecure_verify_skip: str = "true"  # SSL verification for OCP API connections
source_provider_insecure_skip_verify: str = "false"  # SSL verification for source provider (VMware, RHV, etc.)
number_of_vms: int = 1
check_vms_signals: bool = True
target_namespace_prefix: str = "auto"
mtv_namespace: str = "openshift-mtv"
vm_name_search_pattern: str = ""
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
```

> **Note:** `remote_ocp_cluster` is empty by default. If you do not override it, the remote classes are skipped.

The two remote warm plan configs additionally disable preflight Deep Inspection:

```38:48:tests/tests_config/config.py
"test_warm_remote_ocp": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel8",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
    "run_preflight_inspection": False,
},
```

Remote runs still use the same cluster connection inputs as the default flow. The OpenShift client is built from `cluster_host`, `cluster_username`, and `cluster_password`, each
falling back to the matching `CLUSTER_*` environment variable:

```811:832:utilities/utils.py
host = get_value_from_py_config("cluster_host")
if host is None:
    host = os.environ.get("CLUSTER_HOST")

username = get_value_from_py_config("cluster_username")
if username is None:
    username = os.environ.get("CLUSTER_USERNAME")

password = get_value_from_py_config("cluster_password")
if password is None:
    password = os.environ.get("CLUSTER_PASSWORD")
verify_ssl_env = os.environ.get("CLUSTER_VERIFY_SSL")
if verify_ssl_env is not None:
    insecure_verify_skip = verify_ssl_env.lower() not in ("true", "1", "yes")
else:
    insecure_verify_skip = get_value_from_py_config("insecure_verify_skip")
    if insecure_verify_skip is None:
        insecure_verify_skip = True
client = get_client(host=host, username=username, password=password, verify_ssl=not insecure_verify_skip)
return client
```

Treat the inputs like this:

- `cluster_host`, `cluster_username`, `cluster_password`: identify and authenticate to the OpenShift cluster used by the test session.
- `remote_ocp_cluster`: enables the remote test classes and checks that `cluster_host` points at the expected cluster.
- `source_provider`: still selects the source side from `.providers.json`.
- `storage_class`: still controls where migrated VM disks land.
- `snapshots_interval`, `mins_before_cutover`, and `plan_wait_timeout`: still apply because remote runs share the same warm/cold migration helpers.

> **Warning:** `remote_ocp_cluster` is both a gate and a sanity check. In `conftest.py`, the `ocp_admin_client` fixture fails early if the configured value does not appear in the
> connected API host:

```541:543:conftest.py
if remote_cluster_name := get_value_from_py_config("remote_ocp_cluster"):
    if remote_cluster_name not in _client.configuration.host:
        raise RemoteClusterAndLocalCluterNamesError("Remote cluster must be the same as local cluster.")
```

## The Migration Path After Provider Selection

Once the suite has picked the destination provider, the rest of the path is shared. The same helper code in `utilities/mtv_migration.py` threads the selected destination provider
into the `Plan`.

```281:296:utilities/mtv_migration.py
plan_kwargs: dict[str, Any] = {
    "client": ocp_admin_client,
    "fixture_store": fixture_store,
    "resource": Plan,
    "namespace": target_namespace,
    "source_provider_name": source_provider.ocp_resource.name,
    "source_provider_namespace": source_provider.ocp_resource.namespace,
    "destination_provider_name": destination_provider.ocp_resource.name,
    "destination_provider_namespace": destination_provider.ocp_resource.namespace,
    "storage_map_name": storage_map.name,
    "storage_map_namespace": storage_map.namespace,
    "network_map_name": network_map.name,
    "network_map_namespace": network_map.namespace,
    "virtual_machines_list": vms_for_plan,
    "target_namespace": vm_target_namespace or target_namespace,
    "warm_migration": warm_migration,
```

That is why remote scenarios behave so much like local ones:

- The same helper creates the `Plan`.
- The same `execute_migration()` function creates the `Migration` CR and waits for completion.
- The same `check_vms()` validation path is used afterward.

This is the most useful mental model for users: in `mtv-api-tests`, remote OpenShift migration is mainly a destination-provider swap, not a separate migration architecture.

## Warm and Cold Remote Scenarios

The checked-in remote coverage is currently split across four classes:

- `TestWarmRemoteOcp` and `TestColdRemoteOcp` use `destination_ocp_provider`.
- `TestClusterroleWarmMtvMigration` and `TestClusterroleWarmWithSccMigration` use `clusterrole_destination_ocp_provider`.

The warm remote path keeps the normal warm-migration behavior:

- It uses `precopy_interval_forkliftcontroller`.
- It uses `get_cutover_value()`, which is driven by `mins_before_cutover`.
- It uses the same post-migration validation flow as the local warm path.

> **Warning:** Remote warm migration does not bypass the repository's normal warm-migration limits. Warm tests are still skipped at collection time for `openstack`, `openshift`,
> `ova`, and `hyperv` source providers by the hook in the root `conftest.py`.

The cold remote path also stays close to the default flow: it creates the same resources, executes the same migration helper, and runs the same validation logic after completion.
Its plan config migrates two VMs, one RHEL and one Windows:

```56:64:tests/tests_config/config.py
"test_cold_remote_ocp": {
    "virtual_machines": [
        {"name": "mtv-tests-rhel8"},
        {
            "name": "mtv-win2019-79",
        },
    ],
    "warm_migration": False,
},
```

## How Remote Differs From the Default Local Destination Flow

| Area | Default local destination flow | Remote OpenShift flow |
| --- | --- | --- |
| Test selection | Standard classes | `@pytest.mark.remote` classes |
| Config gate | None | `remote_ocp_cluster` must be set |
| Destination provider | Local OpenShift provider with empty `url` and empty `secret` | Explicit OpenShift provider with API `url` and token-backed `Secret` |
| Cluster connection | `cluster_host`, `cluster_username`, `cluster_password` | Same values |
| Source provider config | `.providers.json` | Same `.providers.json` source config |
| Plan / StorageMap / NetworkMap helpers | Shared | Same shared helpers |
| Warm behavior | Shared warm logic | Same shared warm logic |

> **Tip:** When you troubleshoot or document a remote scenario, start with provider creation and config values first. Most of the remote-specific behavior is in destination
> provider setup, not in the migration execution steps.

## Automation and Job Patterns

This repository does not include a dedicated remote-specific pipeline, workflow, or job file. The closest checked-in automation example is the OpenShift Job pattern in
`guides/copyoffload/how-to-run-copyoffload-tests.md`, which shows how the project passes cluster settings into `pytest`.

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

That example is not remote-specific, but it shows the exact configuration pattern the project uses:

- runtime values are injected with `--tc=...`
- source-side selection still happens with `source_provider`
- cluster-side selection still happens with `cluster_host`, `cluster_username`, and `cluster_password`

For remote scenarios, the extra remote-specific value is `remote_ocp_cluster`, and test selection happens through the `remote` marker rather than a special runner script.

## Practical Guidance

- Keep `.providers.json` focused on the source provider. The destination OpenShift provider is created by fixtures in `conftest.py` and `tests/warm/conftest.py`.
- Treat `remote_ocp_cluster` as both an enable switch and a hostname sanity check.
- Expect remote warm and remote cold runs to behave like their local equivalents after the destination provider has been created.
- If the `forklift-migrator-role` ClusterRole is not present in the cluster, the two `TestClusterrole*` classes cannot run; the fixture deliberately does not create it.
- If you need to automate remote runs, follow the repository's existing `--tc=` pattern rather than looking for a separate remote-only pipeline in this checkout.

In short, the remote OpenShift path in `mtv-api-tests` keeps the normal MTV test lifecycle but replaces the implicit local destination provider with an explicit OpenShift
provider built from the active cluster's API endpoint and token.
