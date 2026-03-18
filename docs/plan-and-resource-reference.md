# Plan And Resource Reference

The suite builds the same MTV resource chain you would create by hand: `Provider`, `StorageMap`, `NetworkMap`, `Plan`, and `Migration`. For specific scenarios it also creates `Hook`, `Secret`, `Namespace`, and `NetworkAttachmentDefinition` resources so the plan can run end to end.

> **Note:** The dictionaries in `tests/tests_config/config.py` are not raw CR YAML. Some keys become CR fields, while others only control setup or validation. For example, `clone`, `guest_agent`, and `source_vm_power` affect fixture behavior, not the final `Plan` spec.

## Lifecycle Overview

| Resource | Why the suite creates it | Default placement | Extra readiness rule |
| --- | --- | --- | --- |
| `Namespace` | Isolate each run and any optional VM/NAD namespaces | Per-run `target_namespace`, plus optional custom namespaces | Waits for `Active` |
| `Secret` | Hold provider credentials, OCP token, and copy-offload storage credentials | Usually the per-run `target_namespace` | Created with deploy wait only |
| `Provider` | Represent source and destination endpoints for MTV | Per-run `target_namespace` | Source provider waits for `Ready`; VMware SSH copy-offload also waits for `Validated=True` after patching |
| `StorageMap` | Map source storage to the destination storage class or offload plugin | Per-run `target_namespace` | Created with deploy wait only |
| `NetworkMap` | Map source NICs to pod or Multus networks | Per-run `target_namespace` | Created with deploy wait only |
| `Hook` | Run pre- or post-migration Ansible playbooks | Per-run `target_namespace` | Config is validated before creation |
| `Plan` | Tie providers, mappings, VMs, and optional plan settings together | Per-run `target_namespace` | Waits for `Ready=True` |
| `Migration` | Execute a `Plan` | Per-run `target_namespace` | The suite watches the `Plan` until it becomes `Succeeded` or `Failed` |

## Where Resources Live

| Namespace | What the suite puts there |
| --- | --- |
| `target_namespace` | `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Migration`, `Hook`, provider `Secret`s, and copy-offload storage `Secret`s |
| `vm_target_namespace` | Migrated VMs only, when you set `vm_target_namespace` |
| `multus_namespace` | Extra `NetworkAttachmentDefinition`s, when you set `multus_namespace` |
| `${session_uuid}-source-vms` | Temporary source CNV VMs for OpenShift source-provider tests |

> **Note:** The suite uses `mtv_namespace` for Forklift operator objects such as the inventory route and `ForkliftController`. By default that namespace is `openshift-mtv`. The test-owned migration CRs themselves are created in the per-run `target_namespace`.

## Shared Naming Rules

Every test-owned OpenShift resource goes through `create_and_store_resource()` in `utilities/resources.py`. That helper is the source of most naming, waiting, and cleanup behavior.

```python
if not _resource_name:
    _resource_name = generate_name_with_uuid(name=fixture_store["base_resource_name"])

    if resource.kind in (Migration.kind, Plan.kind):
        _resource_name = f"{_resource_name}-{'warm' if kwargs.get('warm_migration') else 'cold'}"

if len(_resource_name) > 63:
    LOGGER.warning(f"'{_resource_name=}' is too long ({len(_resource_name)} > 63). Truncating.")
    _resource_name = _resource_name[-63:]

kwargs["name"] = _resource_name

_resource = resource(**kwargs)

try:
    _resource.deploy(wait=True)
except ConflictError:
    LOGGER.warning(f"{_resource.kind} {_resource_name} already exists, reusing it.")
    _resource.wait()

LOGGER.info(f"Storing {_resource.kind} {_resource.name} in fixture store")
_resource_dict = {"name": _resource.name, "namespace": _resource.namespace, "module": _resource.__module__}
fixture_store["teardown"].setdefault(_resource.kind, []).append(_resource_dict)
```

A few practical rules fall out of that helper:

- Auto-generated names start from `base_resource_name`, which is built as `{session_uuid}-source-{provider_type}-{version}`.
- Copy-offload source providers add `-xcopy` to that base name.
- Auto-generated `Plan` and `Migration` names also add `-warm` or `-cold`.
- If a name is longer than 63 characters, the helper keeps the last 63 characters so the unique suffix survives.
- If you pass an explicit `name`, or a `kind_dict`/`yaml_file` that already contains one, that name wins.
- Every created resource is tracked for teardown.

The suite also applies related naming rules outside CR creation:

- `session_uuid` comes from `generate_name_with_uuid("auto")`, so generated run identifiers look like `auto-xxxx`.
- `target_namespace_prefix` defaults to `auto`; the fixture strips the literal `auto` before appending it to `session_uuid` so default runs do not produce doubled prefixes.
- Destination VM lookups on OpenShift are sanitized to DNS-1123 format, so names with uppercase letters, `_`, or `.` are converted before lookup.

> **Tip:** When you are debugging a run, search by the session UUID first. Even when names are truncated, the suite preserves the unique suffix rather than the human-friendly prefix.

## Provider

Source provider definitions come from the repo-root `.providers.json`. The loader in `utilities/utils.py` reads that file, and the `source_provider` pytest config value chooses one top-level entry by name.

From `.providers.json.example`:

```text
"vsphere": {
  "type": "vsphere",
  "version": "<SERVER VERSION>",
  "fqdn": "SERVER FQDN/IP",
  "api_url": "<SERVER FQDN/IP>/sdk",
  "username": "USERNAME",
  "password": "PASSWORD",  # pragma: allowlist secret
  "guest_vm_linux_user": "LINUX VMS USERNAME",
  "guest_vm_linux_password": "LINUX VMS PASSWORD",  # pragma: allowlist secret
  "guest_vm_win_user": "WINDOWS VMS USERNAME",
  "guest_vm_win_password": "WINDOWS VMS PASSWORD",  # pragma: allowlist secret
  "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
},
```

The example file supports these provider types:

- `vsphere`
- `ovirt`
- `openstack`
- `openshift`
- `ova`

> **Note:** `.providers.json.example` is a template, not literal JSON. It contains inline comments, so you need to clean those up before using it as a real `.providers.json`.

> **Warning:** The suite fails early if `.providers.json` is missing, empty, or if `source_provider` does not exactly match one of the top-level keys in that file.

Provider creation behavior is slightly different depending on the role:

- Source providers usually follow a two-step flow: create a `Secret`, then create a `Provider` CR that references it.
- The default destination provider is a local OpenShift provider named like `${session_uuid}-local-ocp-provider`.
- Remote-cluster tests create a destination OCP token `Secret` and a provider named like `${session_uuid}-destination-ocp-provider`.

Provider readiness rules are stricter than most other resources:

- The source `Provider` must reach `Ready` within 600 seconds.
- The wait stops early if the provider reports `ConnectionFailed`.
- After the `Provider` exists, Forklift inventory must expose it before the suite can build storage and network mappings.
- For VMware copy-offload with `esxi_clone_method: "ssh"`, the suite patches the provider with `spec.settings.esxiCloneMethod: ssh` and then waits for `Validated=True`.

## Mapping Source Identifiers

The suite does not hard-code one universal `source` shape for mappings. Instead, it asks provider-specific inventory adapters in `libs/forklift_inventory.py` for the right source identifiers.

| Source provider type | `StorageMap` source shape | `NetworkMap` source shape |
| --- | --- | --- |
| `vsphere` | datastore `name` | network `name` |
| `ovirt` | storage domain `name` | network `path` |
| `openstack` | volume type `name` | network `id` and `name` |
| `openshift` | storage class `name` | `{"type": "pod"}` or Multus `networkName` |
| `ova` | storage `id` | network `name` |

> **Note:** OpenStack inventory is treated a little more carefully than the others. The suite waits not only for the VM to appear in Forklift inventory, but also for its attached volumes and networks to become queryable before it builds mappings.

## StorageMap

A standard `StorageMap` is inventory-driven: the suite asks Forklift which source storages the chosen VMs use, then maps each one to the destination `storage_class`.

> **Warning:** `storage_class` is not defined in `tests/tests_config/config.py`. The suite expects it from pytest config, and `get_storage_migration_map()` falls back to `py_config["storage_class"]`.

For copy-offload scenarios, the `StorageMap` carries an offload plugin configuration instead of relying only on source inventory.

From `tests/test_copyoffload_migration.py`:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}

self.__class__.storage_map = get_storage_migration_map(
    fixture_store=fixture_store,
    target_namespace=target_namespace,
    source_provider=source_provider,
    destination_provider=destination_provider,
    ocp_admin_client=ocp_admin_client,
    source_provider_inventory=source_provider_inventory,
    vms=vms_names,
    storage_class=storage_class,
    datastore_id=datastore_id,
    offload_plugin_config=offload_plugin_config,
    access_mode="ReadWriteOnce",
    volume_mode="Block",
)
```

In practice, that means:

- Standard mode maps each source storage to `{"destination": {"storageClass": <storage_class>}, "source": <inventory value>}`.
- Copy-offload mode maps datastores by ID and adds `offloadPlugin`, `accessMode`, and `volumeMode`.
- Mixed and multi-datastore copy-offload are supported through `secondary_datastore_id` and `non_xcopy_datastore_id`.

> **Warning:** `secondary_datastore_id` and `non_xcopy_datastore_id` are only valid when `datastore_id` is also set. The helper rejects those combinations otherwise.

The copy-offload storage secret is also test-owned. Its credentials can come from environment variables or from the provider’s `copyoffload` section in `.providers.json`, and vendor-specific keys are validated before the secret is created.

## NetworkMap

`NetworkMap` creation is intentionally simple and predictable:

- The first source network always maps to the pod network.
- Every additional source network maps to a Multus `NetworkAttachmentDefinition`.
- Extra NADs are named from a short class hash so parallel tests do not collide.

From `utilities/utils.py`:

```python
network_map_list: list[dict[str, dict[str, str]]] = []
_destination_pod: dict[str, str] = {"type": "pod"}
multus_counter = 1

for index, network in enumerate(source_provider_inventory.vms_networks_mappings(vms=vms)):
    if pod_only or index == 0:
        _destination = _destination_pod
    else:
        multus_network_name_str = multus_network_name["name"]
        multus_namespace = multus_network_name["namespace"]

        nad_name = f"{multus_network_name_str}-{multus_counter}"

        _destination = {
            "name": nad_name,
            "namespace": multus_namespace,
            "type": "multus",
        }
        multus_counter += 1

    network_map_list.append({
        "destination": _destination,
        "source": network,
    })
```

That logic ties directly to the class-scoped NAD fixture in `conftest.py`:

- The base NAD name is `cb-<6-char-sha256>`.
- Additional NADs become `cb-<hash>-1`, `cb-<hash>-2`, and so on.
- The names are kept short to stay under Linux bridge interface limits.
- If you set `multus_namespace`, the suite creates or reuses that namespace and puts the NADs there. Otherwise they go into the main `target_namespace`.

> **Tip:** A one-NIC VM still gets a `NetworkMap`, but it does not need any extra `NetworkAttachmentDefinition` objects because the first network always maps to the pod network.

## Hook

Hooks are optional, but when you configure them the suite creates real `Hook` CRs and wires their generated names into the `Plan`.

From `tests/tests_config/config.py`:

```python
"test_post_hook_retain_failed_vm": {
    "virtual_machines": [
        {
            "name": "mtv-tests-rhel8",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "target_power_state": "off",
    "pre_hook": {"expected_result": "succeed"},
    "post_hook": {"expected_result": "fail"},
    "expected_migration_result": "fail",
},
```

Hook configuration supports two modes:

- Predefined mode: set `expected_result` to `succeed` or `fail`, and the suite chooses one of the built-in base64-encoded Ansible playbooks from `utilities/hooks.py`.
- Custom mode: set `playbook_base64` to your own base64-encoded playbook.

Validation rules are strict:

- `expected_result` and `playbook_base64` are mutually exclusive.
- You must set one of them.
- Empty strings are rejected.
- Custom playbooks must be valid base64, valid UTF-8, valid YAML, and a non-empty Ansible play list.

> **Note:** The suite reads detailed hook failure steps from the owned `Migration` CR, not from the `Plan`, because the VM pipeline status lives there. A failing `PostHook` still leads to VM validation, while a failing `PreHook` skips VM checks because the migration never reached the VM validation stage.

## Plan

The `Plan` is where all of the pieces come together. Before the suite creates it, `prepared_plan` may clone VMs, adjust source power state, wait for cloned VMs to appear in Forklift inventory, and call `populate_vm_ids()` so each VM entry has the inventory ID Forklift expects.

The most feature-rich plan-style config in the repo looks like this.

From `tests/tests_config/config.py`:

```python
"test_cold_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2019-3disks",
            "source_vm_power": "off",
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "pvc_name_template": "{{.VmName}}-disk-{{.DiskIndex}}",
    "pvc_name_template_use_generate_name": False,
    "target_node_selector": {
        "mtv-comprehensive-node": None,  # None = auto-generate with session_uuid
    },
    "target_labels": {
        "mtv-comprehensive-label": None,  # None = auto-generate with session_uuid
        "test-type": "comprehensive",  # Static value
    },
    "target_affinity": {
        "podAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": "test"}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 50,
                }
            ]
        }
    },
    "vm_target_namespace": "mtv-comprehensive-vms",
    "multus_namespace": "default",  # Cross-namespace NAD access
},
```

Those settings break down into a few practical groups:

- `warm_migration` controls whether the plan is warm or cold and also changes the auto-generated `Plan` and `Migration` name suffixes.
- `target_power_state`, `preserve_static_ips`, `pvc_name_template`, `pvc_name_template_use_generate_name`, `target_node_selector`, `target_labels`, `target_affinity`, and `vm_target_namespace` are passed into `create_plan_resource()`.
- `multus_namespace` is not a `Plan` field. It tells the NAD fixture where to create extra Multus networks before the `Plan` is built.
- `target_node_selector` and `target_labels` treat `None` specially. The fixtures replace `None` with the current `session_uuid`, which gives you unique labels and selectors without hard-coding a value.

`pvc_name_template` is especially useful when you want predictable PVC names:

- The validation helper supports `{{.VmName}}`, `{{.DiskIndex}}`, and VMware-only `{{.FileName}}`.
- It also supports Sprig functions, because validation uses a Go template renderer.
- Long generated names are truncated during validation to match Kubernetes limits.
- When `pvc_name_template_use_generate_name` is `True`, the suite checks only the generated prefix because Kubernetes adds its own random suffix.

> **Tip:** Use `None` in `target_node_selector` and `target_labels` when you want uniqueness without inventing a new value yourself. The suite will replace it with the run’s `session_uuid`.

After creation, the plan has its own readiness rules:

- The suite waits for `Plan.Condition.READY=True` with a 360-second timeout.
- On timeout, it logs the `Plan` plus both source and destination provider objects to make provider-side issues easier to debug.
- For copy-offload plans, it also waits up to 60 seconds for Forklift to create a plan-specific secret whose name starts with `<plan-name>-`.

> **Note:** The `Plan` CR itself stays in the main `target_namespace` even when `vm_target_namespace` sends migrated VMs somewhere else. Only the migrated VMs move.

> **Note:** The copy-offload secret wait is best-effort. If the `<plan-name>-*` secret is late, the suite continues anyway because the later migration failure is usually more actionable than a generic “secret missing” timeout.

## Migration

`execute_migration()` creates a separate `Migration` CR that points at the `Plan`. For cold migrations it passes no cutover time. For warm migrations it computes one from the current UTC time and the configured offset.

From `utilities/migration_utils.py`:

```python
def get_cutover_value(current_cutover: bool = False) -> datetime:
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))
```

The runtime settings that matter most for migration readiness come from `tests/tests_config/config.py`:

| Key | Default | Why it matters |
| --- | --- | --- |
| `mtv_namespace` | `openshift-mtv` | Where the suite expects Forklift operator objects |
| `snapshots_interval` | `2` | Patched into `ForkliftController.spec.controller_precopy_interval` for warm tests |
| `mins_before_cutover` | `5` | Offset used by `get_cutover_value()` for warm migrations |
| `plan_wait_timeout` | `3600` | Timeout for waiting on migration completion via `Plan` status |
| `target_namespace_prefix` | `auto` | Prefix material for the per-run namespace name |

A few implementation details are worth knowing when you debug a `Migration`:

- The suite creates the `Migration` with `plan_name`, `plan_namespace`, and optional `cut_over`.
- It does not use the `Migration` object alone for final success and failure. Instead, it watches the `Plan`.
- The helper treats a plan as `Executing` as soon as it finds an owned `Migration` CR for that `Plan`.
- Final `Succeeded` and `Failed` states come from advisory conditions on the `Plan`.
- Detailed per-VM failure steps such as `PreHook`, `PostHook`, or `DiskTransfer` come from `migration.status.vms[].pipeline[]`.

> **Note:** Before the suite starts creating plans and migrations, it waits up to five minutes for all `forklift-*` pods in `mtv_namespace` to be `Running` or `Succeeded`, and it requires a controller pod to exist.

> **Note:** Warm migration coverage is not universal. `tests/test_mtv_warm_migration.py` explicitly skips warm tests for `openstack`, `openshift`, and `ova` source providers.

## Related Resources and Cleanup

A few supporting resources show up often enough that they are worth calling out directly:

- The main `target_namespace` is labeled with restricted pod-security settings and `mutatevirtualmachines.kubemacpool.io=ignore`.
- Custom namespaces created through `get_or_create_namespace()` are created with the same standard labels and waited to `Active`.
- OpenShift source-provider tests create a separate `${session_uuid}-source-vms` namespace for source-side CNV VMs.
- Copy-offload tests create a storage credential `Secret` in the run namespace and may also rely on the plan-specific secret Forklift creates later.
- Every object created through `create_and_store_resource()` is registered in `fixture_store["teardown"]`.

Cleanup happens in two layers:

- `cleanup_migrated_vms` removes migrated VMs at class teardown, using `vm_target_namespace` when you set one.
- Session teardown removes `Migration`, `Plan`, `Provider`, `Secret`, `StorageMap`, `NetworkMap`, `NetworkAttachmentDefinition`, namespaces, migrated VMs, and even source-side cloned VMs for providers such as VMware, OpenStack, and RHV.

> **Warning:** `--skip-teardown` leaves those resources behind on purpose. Use it only when you want leftover `Plan`, `Migration`, `Provider`, PVC, VM, and namespace objects for debugging.
