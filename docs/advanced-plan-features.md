# Advanced Plan Features

Advanced plan options let you control how MTV creates the destination VM after the basic provider, storage, and network mappings are in place. In this project, you define those options under `tests_params` in `tests/tests_config/config.py`, then pass them into `create_plan_resource()` from your test class.

| Key | What it controls | Example |
| --- | --- | --- |
| `target_power_state` | Final destination VM power state | `"on"` |
| `preserve_static_ips` | Preserve guest static IP configuration | `True` |
| `pvc_name_template` | Destination PVC naming pattern | `"{{.VmName}}-disk-{{.DiskIndex}}"` |
| `pvc_name_template_use_generate_name` | Let Kubernetes append a random suffix | `True` |
| `vm_target_namespace` | Namespace where migrated VMs are created | `"custom-vm-namespace"` |
| `target_node_selector` | Node labels the VM should be scheduled onto | `{"mtv-comprehensive-node": None}` |
| `target_labels` | Labels added to the migrated VM template | `{"static-label": "static-value"}` |
| `target_affinity` | Affinity rules applied to the migrated VM | `{"podAffinity": {...}}` |
| `multus_namespace` | Namespace where extra NADs are created | `"default"` |

## Typical usage

A full example already exists in `tests/tests_config/config.py`:

```python
"test_warm_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2022-ip-3disks",
            "source_vm_power": "on",
            "guest_agent": True,
        },
    ],
    "warm_migration": True,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "vm_target_namespace": "custom-vm-namespace",
    "multus_namespace": "default",  # Cross-namespace NAD access
    "pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
    "pvc_name_template_use_generate_name": True,
    "target_labels": {
        "mtv-comprehensive-test": None,  # None = auto-generate with session_uuid
        "static-label": "static-value",
    },
    "target_affinity": {
        "podAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": "comprehensive-test"}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                    "weight": 75,
                }
            ]
        }
    },
},
```

That configuration is then passed into the Plan helper in `tests/test_warm_migration_comprehensive.py`:

```python
self.__class__.plan_resource = create_plan_resource(
    ocp_admin_client=ocp_admin_client,
    fixture_store=fixture_store,
    source_provider=source_provider,
    destination_provider=destination_provider,
    storage_map=self.storage_map,
    network_map=self.network_map,
    virtual_machines_list=prepared_plan["virtual_machines"],
    target_power_state=prepared_plan["target_power_state"],
    target_namespace=target_namespace,
    warm_migration=prepared_plan["warm_migration"],
    preserve_static_ips=prepared_plan["preserve_static_ips"],
    vm_target_namespace=prepared_plan["vm_target_namespace"],
    pvc_name_template=prepared_plan["pvc_name_template"],
    pvc_name_template_use_generate_name=prepared_plan["pvc_name_template_use_generate_name"],
    target_labels=target_vm_labels["vm_labels"],
    target_affinity=prepared_plan["target_affinity"],
)
```

## Target power state

Use `target_power_state` when you want the migrated VM to end in a known power state, regardless of how the source VM started out. The repository examples use both `"on"` and `"off"`.

If you omit `target_power_state`, the post-migration check falls back to the VM's pre-migration source power state.

> **Note:** `source_vm_power` and `target_power_state` are different. `source_vm_power` controls how the source VM is prepared before migration. `target_power_state` controls the expected state of the destination VM after migration.

Practical examples from `tests/tests_config/config.py` include:

- `"target_power_state": "on"` in both comprehensive migration tests
- `"target_power_state": "off"` in `test_post_hook_retain_failed_vm`

## Static IP preservation

Set `preserve_static_ips` to `True` when you want MTV to preserve guest static IP settings. Both comprehensive migration configs enable it.

For vSphere sources, the project records guest IP origin and treats `origin == "manual"` as a static IP:

```python
if hasattr(ip_info, "origin"):
    ip_config["ip_origin"] = ip_info.origin
    ip_config["is_static_ip"] = ip_info.origin == "manual"
    LOGGER.info(
        f"VM {vm.name} NIC {device.deviceInfo.label}: IPv4={ip_info.ipAddress}"
        f" Origin={ip_info.origin} Static={ip_info.origin == 'manual'}",
    )
```

After migration, the verifier connects to the destination VM over SSH and checks the guest configuration.

> **Warning:** In this repository, static IP verification is currently implemented only for Windows guests migrated from vSphere. The verification path uses `ipconfig /all` inside the guest and does not support non-Windows guests yet.

> **Note:** If no static interfaces are found on the source VM, the check is skipped rather than failed.

## PVC naming templates

Use `pvc_name_template` when you want stable, readable PVC names. The repository covers both exact names and `generateName`-style prefixes.

Examples from `tests/tests_config/config.py`:

```python
"pvc_name_template": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
"pvc_name_template_use_generate_name": True,
```

```python
"pvc_name_template": "{{.VmName}}-disk-{{.DiskIndex}}",
"pvc_name_template_use_generate_name": False,
```

The validator in `utilities/post_migration.py` explicitly supports:

- `{{.VmName}}`
- `{{.DiskIndex}}`
- `{{.FileName}}`
- Sprig functions such as `trimSuffix`, `replace`, `lower`, and `upper`

When `pvc_name_template_use_generate_name` is `True`, the project expects Kubernetes to append a random suffix and validates the PVC by prefix match. When it is `False`, the validator expects an exact name match.

> **Note:** The validator also accounts for Kubernetes name-length limits by truncating the rendered template before comparing it with the actual PVC name.

> **Warning:** In this repository's post-migration validation, templates using `{{.FileName}}` and `{{.DiskIndex}}` are effectively vSphere-oriented. If the source provider is not vSphere, the PVC-name verifier logs a warning and skips that validation path.

There is one important exception for copy-offload migrations. In `utilities/mtv_migration.py`, `copyoffload=True` overrides any custom template:

```python
if copyoffload:
    plan_kwargs["pvc_name_template"] = "pvc"
```

> **Warning:** If you are running copy-offload tests, do not expect your custom `pvc_name_template` to be used. The helper forces it to `"pvc"` so the volume populator framework gets predictable PVC prefixes.

## Custom VM namespaces

Use `vm_target_namespace` when you want the migrated VM to land in a namespace that is different from the namespace where the Plan and mapping resources are created.

The comprehensive tests show two examples:

- `"vm_target_namespace": "custom-vm-namespace"`
- `"vm_target_namespace": "mtv-comprehensive-vms"`

In this project:

- The Plan, `StorageMap`, and `NetworkMap` are still created in the regular `target_namespace`
- The migrated VM itself is created in `vm_target_namespace`
- The `prepared_plan` fixture creates that namespace automatically if it does not already exist
- The post-migration checks also look up the migrated VM in that custom namespace

> **Note:** This is useful when you want one namespace for migration resources and another namespace for the resulting VMs.

## Node selectors, labels, and affinity

These options control where the migrated VM runs and what metadata MTV applies to it.

A cold-migration example from `tests/test_cold_migration_comprehensive.py` shows the scheduling-related arguments passed into the Plan helper:

```python
target_node_selector={labeled_worker_node["label_key"]: labeled_worker_node["label_value"]},
target_labels=target_vm_labels["vm_labels"],
target_affinity=prepared_plan["target_affinity"],
vm_target_namespace=prepared_plan["vm_target_namespace"],
```

### Node selectors

Use `target_node_selector` to place the destination VM on nodes with a matching label.

Example from `tests/tests_config/config.py`:

```python
"target_node_selector": {
    "mtv-comprehensive-node": None,
},
```

In the comprehensive cold test, the `labeled_worker_node` fixture picks a worker node, applies the label, and passes the resolved selector into `create_plan_resource()`. After migration, the project verifies that the VM actually landed on that node.

> **Tip:** In this project, setting the selector value to `None` tells the fixture to replace it with the current `session_uuid`. That makes the label unique for each run and helps avoid collisions in parallel execution.

### Labels

Use `target_labels` to stamp metadata onto the migrated VM template.

Examples from `tests/tests_config/config.py`:

- `"static-label": "static-value"`
- `"mtv-comprehensive-test": None`
- `"test-type": "comprehensive"`

The OpenShift provider reads labels back from the VM template metadata and the post-migration verifier checks that every expected label is present with the expected value.

> **Tip:** As with node selectors, a label value of `None` is replaced by the current `session_uuid`, which is useful when you need unique per-run labels.

### Affinity

Use `target_affinity` when you need Kubernetes scheduling preferences or constraints on the migrated VM.

The repository examples use `podAffinity` with `preferredDuringSchedulingIgnoredDuringExecution`, for example matching VMs near pods with a specific label and topology key.

Because the verifier deep-compares the resulting affinity block, it is best to keep the structure in your test config exactly as MTV should apply it.

> **Note:** In this repository, labels are validated from the VM template metadata and affinity is validated from the VM template spec. Node placement is validated from the running VMI's assigned node.

## Multus setup

Multus support is handled as part of network-map preparation, not as a direct Plan field.

The default Multus CNI configuration comes from `conftest.py`:

```python
@pytest.fixture(scope="session")
def multus_cni_config() -> str:
    bridge_type_and_name = "cnv-bridge"
    config = {"cniVersion": "0.3.1", "type": f"{bridge_type_and_name}", "bridge": f"{bridge_type_and_name}"}
    return json.dumps(config)
```

When the source VM has multiple NICs, `utilities/utils.py` maps the first source network to the pod network and every additional network to a Multus NAD:

```python
for index, network in enumerate(source_provider_inventory.vms_networks_mappings(vms=vms)):
    if pod_only or index == 0:
        # First network or pod_only mode → pod network
        _destination = _destination_pod
    else:
        # Extract base name and namespace from multus_network_name (only when needed)
        multus_network_name_str = multus_network_name["name"]
        multus_namespace = multus_network_name["namespace"]

        # Generate unique NAD name for each additional network
        # Use consistent naming: {base_name}-1, {base_name}-2, etc.
        # Where base_name includes unique test identifier (e.g., cnv-bridge-abc12345)
        nad_name = f"{multus_network_name_str}-{multus_counter}"

        _destination = {
            "name": nad_name,
            "namespace": multus_namespace,
            "type": "multus",
        }
        multus_counter += 1  # Increment for next NAD
```

What this means in practice:

- The first NIC stays on the pod network
- Each additional NIC gets its own `NetworkAttachmentDefinition`
- NADs are named from a short base name plus `-1`, `-2`, and so on
- `multus_namespace` decides where those NADs are created

Both comprehensive configs use:

```python
"multus_namespace": "default",
```

That enables cross-namespace NAD access by creating or reusing the NADs in `default`.

> **Note:** A single-NIC VM does not need Multus. The fixture calculates the number of NADs as `max(0, len(networks) - 1)`, so only additional NICs get Multus attachments.

> **Tip:** If you want to keep migration resources in one namespace but share NADs from another namespace, set `multus_namespace` explicitly, as the comprehensive tests do.
