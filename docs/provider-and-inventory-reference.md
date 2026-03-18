# Provider And Inventory Reference

`mtv-api-tests` uses two sources of truth for source-side data:

- direct provider classes for actions such as connect, clone, power control, snapshots, and VM inspection
- Forklift inventory adapters for the names, IDs, networks, and storages that MTV itself consumes

That split explains most of the repository's behavior. If a problem is about cloning, power state, guest information, or direct SDK access, start with the provider class. If a problem is about `NetworkMap`, `StorageMap`, or a VM ID inside a `Plan`, start with Forklift inventory.

## End-To-End Flow

1. The suite loads `.providers.json` from the repository root.
2. `py_config["source_provider"]` selects one top-level provider entry from that file.
3. `create_source_provider()` creates the source `Secret` and source `Provider` CR, then instantiates the matching `BaseProvider` subclass.
4. `source_provider_inventory` chooses the matching `ForkliftInventory` adapter for that provider type.
5. `prepared_plan` clones or creates source VMs, normalizes them through `vm_dict()`, rewrites the VM name if needed, and waits for Forklift inventory to see the VM.
6. `get_network_migration_map()` and `get_storage_migration_map()` turn inventory data into `NetworkMap` and `StorageMap` CR payloads.
7. `populate_vm_ids()` copies Forklift VM IDs into the `Plan` payload just before `create_plan_resource()` runs.

The synchronization step is explicit in `conftest.py`:

```python
for vm in virtual_machines:
    # Get VM object first (without full vm_dict analysis)
    # Add enable_ctk flag for warm migrations
    clone_options = {**vm, "enable_ctk": warm_migration}
    provider_vm_api = source_provider.get_vm_by_name(
        query=vm["name"],
        vm_name_suffix=vm_name_suffix,
        clone_vm=True,
        session_uuid=fixture_store["session_uuid"],
        clone_options=clone_options,
    )

    source_vm_details = source_provider.vm_dict(
        provider_vm_api=provider_vm_api,
        name=vm["name"],
        namespace=source_vms_namespace,
        clone=False,  # Already cloned above
        vm_name_suffix=vm_name_suffix,
        session_uuid=fixture_store["session_uuid"],
        clone_options=vm,
    )
    vm["name"] = source_vm_details["name"]

    if source_provider.type != Provider.ProviderType.OVA:
        source_provider_inventory.wait_for_vm(name=vm["name"], timeout=300)
```

> **Note:** A map-generation failure often means Forklift inventory has not finished syncing the VM yet. The suite intentionally waits for inventory before creating maps, because MTV consumes inventory objects, not the provider SDK's in-memory objects.

## Provider Configuration And Selection

The selection logic is intentionally simple: `.providers.json` is loaded, and `py_config["source_provider"]` picks one named entry.

```python
@pytest.fixture(scope="session")
def source_provider_data(source_providers: dict[str, dict[str, Any]], fixture_store: dict[str, Any]) -> dict[str, Any]:
    """Resolve source provider configuration from .providers.json."""
    if not source_providers:
        raise MissingProvidersFileError()

    requested_provider = py_config["source_provider"]
    if requested_provider not in source_providers:
        raise ValueError(
            f"Source provider '{requested_provider}' not found in '.providers.json'. "
            f"Available providers: {sorted(source_providers.keys())}"
        )

    _source_provider = source_providers[requested_provider]
    fixture_store["source_provider_data"] = _source_provider
    return _source_provider
```

The repository does not commit a sample `.providers.json`, so the code is the best reference for required fields.

| Field | Why the suite reads it |
| --- | --- |
| `type` | Selects the provider class and the Forklift inventory adapter |
| `version` | Used in generated resource names by `base_resource_name` |
| `api_url` | Written into the source provider secret and the `Provider` CR |
| `username`, `password` | Read before provider-specific branching |
| `fqdn` | Used by the CA certificate helper when certificate download is needed |

The factory in `utilities/utils.py` reads the common connection fields before it knows which provider type it is handling:

```python
secret_string_data = {
    "url": source_provider_data_copy["api_url"],
    "insecureSkipVerify": "true" if insecure else "false",
}
provider_args = {
    "username": source_provider_data_copy["username"],
    "password": source_provider_data_copy["password"],
    "fixture_store": fixture_store,
}
```

Provider-specific extras are then added on top:

| Source type | Extra fields read by code |
| --- | --- |
| `vsphere` | optional `copyoffload`, optional `vddk_init_image` |
| `rhv` | no extra SDK login fields, but the current code still expects `fqdn` for CA cert download |
| `openstack` | `project_name`, `user_domain_name`, `region_name`, `user_domain_id`, `project_domain_id` |
| `openshift` | no extra provider-specific fields; the source uses the destination cluster secret |
| `ova` | no extra provider-specific fields; direct provider logic is intentionally minimal |

> **Warning:** `create_source_provider()` reads `api_url`, `username`, and `password` before provider-specific branching, so every provider entry needs those keys. `version` is also needed for generated resource names. When certificate download is involved, `fqdn` matters too, because the certificate helper always connects to `<fqdn>:443`.

## The BaseProvider Contract

`BaseProvider` is the common surface that keeps the rest of the suite provider-agnostic. Each concrete provider can use its own SDK, but it must expose the same core operations to the test code.

The normalized VM shape is defined in `libs/base_provider.py`:

```python
VIRTUAL_MACHINE_TEMPLATE: dict[str, Any] = {
    "id": "",
    "name": "",
    "provider_type": "",  # "ovirt" / "vsphere" / "openstack"
    "provider_vm_api": None,
    "network_interfaces": [],
    "disks": [],
    "cpu": {},
    "memory_in_mb": 0,
    "snapshots_data": [],
    "power_state": "",
}
```

The most important abstraction for mapping logic is `get_vm_or_template_networks()`:

```python
def get_vm_or_template_networks(
    self,
    names: list[str],
    inventory: ForkliftInventory,
) -> list[dict[str, str]]:
    """Get network mappings for VMs or templates (before cloning).

    This method handles provider-specific differences:
    - RHV: Queries template networks directly (templates don't exist in inventory yet)
    - VMware/OpenStack/OVA/OpenShift: Queries VM networks from Forklift inventory
    """
```

In practice, `BaseProvider` gives the suite five important guarantees:

- `connect()` and `disconnect()` manage the direct SDK session.
- `test` is used immediately after provider creation to fail fast when the source is not reachable.
- `vm_dict()` returns a normalized view of the source or destination VM.
- `clone_vm()` and `delete_vm()` let the suite prepare and clean up source-side test VMs without special-case code in every test.
- `get_vm_or_template_networks()` lets the suite size destination networking even before cloned VMs have finished syncing into inventory.

## Provider-Specific Behavior

### vSphere

`VMWareProvider` in `libs/providers/vmware.py` is the most feature-rich source implementation in the repo.

- It uses `pyVmomi` for clone, power, disk, and guest-information operations.
- Clone-time options cover disk provisioning (`thin`, `thick-lazy`, `thick-eager`), extra disks, datastore overrides, ESXi host overrides, MAC regeneration, and Change Block Tracking for warm migrations.
- If `copyoffload.esxi_clone_method` is set to `ssh`, the provider patches the MTV `Provider` CR to set `esxiCloneMethod`.
- When copy-offload is configured, the source `Provider` CR also gets the annotation `forklift.konveyor.io/empty-vddk-init-image: yes`.
- Standard network and storage mappings come from `VsphereForkliftInventory`. Copy-offload storage mappings can bypass inventory and use explicit datastore IDs instead.

> **Note:** The test-side vSphere SDK connection uses `disableSslCertValidation=True`, while the Forklift `Provider` CR still honors `source_provider_insecure_skip_verify` and can include `cacert`. That means direct provider access and MTV-side validation are related, but not identical, code paths.

### RHV / oVirt

`OvirtProvider` in `libs/providers/rhv.py` has two important behaviors that are easy to miss.

- It refuses to connect unless the RHV datacenter named `MTV-CNV` exists and is `up`.
- `clone_vm()` clones from a template, not from a running VM. In other words, `source_vm_name` is treated as a template name in RHV flows.
- `get_vm_or_template_networks()` ignores inventory during pre-clone network discovery and queries template NICs directly.
- Later, once cloned VMs exist and Forklift has synced them, final `NetworkMap` and `StorageMap` generation still comes from inventory.
- RHV always fetches a CA certificate in `create_source_provider()`, even when insecure mode is enabled, because the code comments call out imageio as a dependency for that certificate.

### OpenStack

`OpenStackProvider` in `libs/providers/openstack.py` clones more like an image pipeline than a simple VM copy.

- It snapshots the source server.
- It creates new volumes from the resulting snapshots.
- It preserves boot order across the recreated volumes.
- It boots the cloned server from those new volumes.

OpenStack is also the strictest inventory-sync path. The suite does not treat “VM exists in inventory” as enough. It also waits for attached volumes and networks to become queryable:

```python
if self.provider_type == Provider.ProviderType.OPENSTACK:
    if not (
        self._check_openstack_volumes_synced(vm, name)
        and self._check_openstack_networks_synced(vm, name)
    ):
        return None
```

That matters because OpenStack storage mapping is based on volume type, and network mapping is based on inventory network objects matched against VM address data. If either side is late, map generation will be wrong.

### OpenShift

`OCPProvider` is both the destination provider for migrations and a supported source provider for source-side CNV test setups.

- If OpenShift is the source, `prepared_plan` creates source CNV VMs and a source-side `NetworkAttachmentDefinition` automatically.
- `OpenshiftForkliftInventory` resolves storage by following the VM's data volumes to PVCs and then reading `storageClassName`.
- It resolves networks from the VM template: `multus.networkName` becomes a named source network, and a pod network becomes `{"type": "pod"}`.
- On destination lookups, `OCPProvider.vm_dict()` sanitizes VM names to Kubernetes-safe resource names before querying the cluster.

### OVA

`OVAProvider` is intentionally thin.

- `connect()` is effectively a no-op.
- `clone_vm()` and `delete_vm()` are not implemented.
- `vm_dict()` only fills the normalized template with the provider type and `power_state="off"`.
- In the current `prepared_plan` implementation, OVA sources are rewritten to the fixed VM name `1nisim-rhel9-efi`.
- In practice, OVA mapping behavior depends much more on Forklift inventory than on direct provider logic.

> **Note:** The warm-migration test suites currently skip OpenStack, OpenShift, and OVA sources. RHV warm coverage is gated by a Jira marker. In this repository, vSphere is the fully exercised warm-migration source.

## Forklift Inventory Adapters

Forklift inventory lives behind the `forklift-inventory` route in the MTV namespace. Each adapter subclasses `ForkliftInventory` and knows how to translate that provider's inventory model into the source-side names and IDs that MTV map CRs expect.

The adapter selection is defined in `conftest.py`:

```python
providers = {
    Provider.ProviderType.OVA: OvaForkliftInventory,
    Provider.ProviderType.RHV: OvirtForkliftInventory,
    Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
    Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
    Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
}
provider_instance = providers.get(source_provider.type)
```

The adapters all use the same base route plumbing in `libs/forklift_inventory.py`: they discover the provider ID, build a provider-specific URL path, and then query VM, network, and storage endpoints through the inventory service.

| Source type | Adapter | Storage resolution | Network resolution |
| --- | --- | --- | --- |
| vSphere | `VsphereForkliftInventory` | Each disk's `datastore.id` is matched to an inventory datastore name | Each inventory VM network `id` is matched to an inventory network name |
| RHV | `OvirtForkliftInventory` | Disk attachment -> disk -> `storageDomain` -> storage domain name | NIC profile -> network ID -> inventory network `path` |
| OpenStack | `OpenstackForliftinventory` | Attached volume -> volume details -> `volumeType` | VM `addresses` keys are matched to inventory network names |
| OpenShift | `OpenshiftForkliftInventory` | VM data volumes -> PVC -> `storageClassName` | VM template networks become either named multus networks or `{"type": "pod"}` |
| OVA | `OvaForkliftInventory` | Inventory storage entries whose name contains the VM name; mapping returns storage `id` | Inventory VM network `ID` is matched to an inventory network name |

Two adapter details are especially practical:

- RHV uses provider-side template queries for pre-clone network discovery, but inventory for final map generation.
- OpenShift storage resolution is not just “whatever inventory says.” The adapter actually follows data volumes to PVCs and reads the live `storageClassName`.

## How Source Network Mappings Are Resolved

Network mapping is a two-step process.

First, the suite decides how many destination networks it needs. It does that in the `multus_network_name` fixture by calling `source_provider.get_vm_or_template_networks()`. This is why RHV can use template networks before clones exist.

Second, once the cloned or prepared source VM has been synced into Forklift inventory, the actual `NetworkMap` payload is built from inventory data.

The core rule lives in `utilities/utils.py`:

```python
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

What that means in plain language:

1. The first source network is always mapped to the destination pod network.
2. Every additional source network is mapped to a generated Multus NAD.
3. Those NADs are named from a base name plus a numeric suffix: `{base}-1`, `{base}-2`, and so on.
4. If the plan config sets `multus_namespace`, the NADs are created there instead of in the main target namespace.

> **Warning:** Network mapping is order-based. The first source network returned by inventory becomes the pod network, so inventory ordering matters for multi-NIC VMs.

> **Tip:** If a source VM has `N` networks, the suite creates `N - 1` NADs, because the first network is reserved for the destination pod network.

## How Source Storage Mappings Are Resolved

The storage path is simpler than the network path in standard migrations: the helper trusts the selected inventory adapter to tell it which source storage objects matter, then it adds the destination storage class.

The standard branch of `get_storage_migration_map()` looks like this:

```python
storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
for storage in storage_migration_map:
    storage_map_list.append({
        "destination": {"storageClass": target_storage_class},
        "source": storage,
    })
```

A few practical consequences follow from that:

- The destination storage class is `py_config["storage_class"]` unless the caller passes an explicit `storage_class` argument.
- The meaning of the source side is provider-specific. On vSphere it is a datastore. On RHV it is a storage domain. On OpenStack it is a volume type. On OpenShift it is a storage class. On OVA it is a storage ID.
- If the adapter returns the wrong source identifier, the `StorageMap` will be wrong even if the direct provider SDK can see the disks just fine.

### vSphere Copy-Offload

Copy-offload is the main exception to inventory-derived storage mapping. In copy-offload mode, `get_storage_migration_map()` can build the source side from explicit datastore IDs instead of asking inventory.

```python
if datastore_id and offload_plugin_config:
    datastores_to_map = [datastore_id]
    if secondary_datastore_id:
        datastores_to_map.append(secondary_datastore_id)

    for ds_id in datastores_to_map:
        destination_config = {
            "storageClass": target_storage_class,
        }

        if access_mode:
            destination_config["accessMode"] = access_mode
        if volume_mode:
            destination_config["volumeMode"] = volume_mode

        storage_map_list.append({
            "destination": destination_config,
            "source": {"id": ds_id},
            "offloadPlugin": offload_plugin_config,
        })
else:
    storage_migration_map = source_provider_inventory.vms_storages_mappings(vms=vms)
    for storage in storage_migration_map:
        storage_map_list.append({
            "destination": {"storageClass": target_storage_class},
            "source": storage,
        })
```

The repo validates copy-offload prerequisites in `conftest.py`:

```python
required_credentials = ["storage_hostname", "storage_username", "storage_password"]
required_params = ["storage_vendor_product", "datastore_id"]
```

In practice, the copy-offload flow expects:

- `storage_vendor_product`
- `datastore_id`
- `storage_hostname`
- `storage_username`
- `storage_password`

Optional extensions used by the code include:

- `secondary_datastore_id`
- `non_xcopy_datastore_id`
- `esxi_clone_method`
- `esxi_host`
- `esxi_user`
- `esxi_password`

The storage secret builder also knows vendor-specific extras. Depending on `storage_vendor_product`, the repo may look for keys such as `ontap_svm`, `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list`, `pure_cluster_prefix`, `powerflex_system_id`, or `powermax_symmetrix_id`.

The test suite builds the offload plugin config like this:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}
```

When `copyoffload=True`, `create_plan_resource()` also forces `pvc_name_template` to `"pvc"` because the volume-populator path expects predictable PVC naming.

> **Warning:** Copy-offload in this repository is a vSphere-specific path. The validation fixture explicitly fails if the selected source provider is not vSphere.

> **Tip:** Copy-offload credentials can come from environment variables as well as `.providers.json`. The code looks for names such as `COPYOFFLOAD_STORAGE_HOSTNAME`, `COPYOFFLOAD_STORAGE_USERNAME`, and `COPYOFFLOAD_STORAGE_PASSWORD`, plus vendor-specific `COPYOFFLOAD_<FIELD>` overrides.

## Inventory IDs And PVC Naming

Forklift inventory does two more important jobs after maps are built.

First, it provides the VM IDs that go into the migration `Plan`:

```python
def populate_vm_ids(plan: dict[str, Any], inventory: ForkliftInventory) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("virtual_machines"), list):
        raise ValueError("plan must contain 'virtual_machines' list")

    for vm in plan["virtual_machines"]:
        vm_name = vm["name"]
        vm_data = inventory.get_vm(vm_name)
        vm["id"] = vm_data["id"]
```

Second, it can supply disk filenames for PVC-template validation. In `utilities/post_migration.py`, the `{{.FileName}}` wildcard is resolved from Forklift inventory disk file paths, not from the direct provider SDK. That validation path is only enabled for vSphere sources.

> **Tip:** If you use `pvc_name_template` with `{{.FileName}}`, you are depending on inventory disk metadata. The suite sorts vSphere source disks by `(controller_key, unit_number)` before it renders expected PVC names.

## Real Test Config Examples

These are exact snippets from `tests/tests_config/config.py`. They are useful because they show the real shapes the repository already exercises.

### Comprehensive Warm Migration Example

This example combines custom VM namespace placement, cross-namespace Multus, static IP preservation, and a PVC naming template based on inventory file names.

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

### Mixed-Datastore Copy-Offload Example

This example shows a vSphere copy-offload plan where an added disk is intentionally placed on a non-XCOPY datastore.

```python
"test_copyoffload_mixed_datastore_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "source_vm_power": "off",
            "guest_agent": True,
            "clone": True,
            "disk_type": "thin",
            "add_disks": [
                {
                    "size_gb": 30,
                    "provision_type": "thin",
                    "datastore_id": "non_xcopy_datastore_id",
                },
            ],
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
},
```

> **Warning:** Symbolic datastore values such as `secondary_datastore_id` and `non_xcopy_datastore_id` are not global magic strings. The vSphere provider resolves them from the selected provider's `copyoffload` configuration.

Once you know which data comes from the direct provider class and which data comes from Forklift inventory, the rest of the repository becomes much easier to predict. Provider classes explain how source VMs are created and inspected. Inventory adapters explain how MTV sees those VMs. The map helpers then turn that inventory view into the exact `StorageMap`, `NetworkMap`, and `Plan` payloads that the migration uses.
