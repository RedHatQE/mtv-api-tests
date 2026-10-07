# Provider And Inventory Reference

`mtv-api-tests` uses two sources of truth for source-side data:

- direct provider classes for actions such as connect, clone, power control, snapshots, and VM inspection
- Forklift inventory adapters for the names, IDs, networks, and storages that MTV itself consumes

That split explains most of the repository's behavior. If a problem is about cloning, power state, guest information, or direct SDK access, start with the provider class. If a
problem is about `NetworkMap`, `StorageMap`, or a VM ID inside a `Plan`, start with Forklift inventory.

## End-To-End Flow

1. The suite loads `.providers.json` from the repository root through `resolve_providers_json_path()` and `load_source_providers()` (`utilities/utils.py:77`).
2. `py_config["source_provider"]` selects one top-level provider entry from that file (`conftest.py:718`).
3. `create_source_provider()` creates the source `Secret` and source `Provider` CR, waits for `Provider.Status.READY`, then instantiates the matching `BaseProvider` subclass as a
   context manager (`utilities/utils.py:371`).
4. The `source_provider_inventory` fixture calls `create_forklift_inventory()`, which picks the adapter registered for `source_provider.type` (`conftest.py:1712`,
   `libs/forklift_inventory.py:33`).
5. `prepared_plan` clones or creates source VMs, normalizes them through `vm_dict()`, and rewrites the VM name to the cloned name (`conftest.py:1310`).
6. After **every** clone finishes, the fixture waits for all cloned VMs to appear in Forklift inventory (`conftest.py:1367`).
7. `get_network_migration_map()` and `get_storage_migration_map()` turn inventory data into `NetworkMap` and `StorageMap` CR payloads (`utilities/mtv_migration.py:687`,
   `utilities/mtv_migration.py:548`).
8. `populate_vm_ids()` copies Forklift VM IDs into the `Plan` payload just before `create_plan_resource()` runs (`utilities/utils.py:911`).

The synchronization step is explicitly two-phase in `conftest.py`, because waiting per-VM inside the clone loop caused inventory sync failures on the second and later VMs:

```python
# Phase 2: wait for all cloned VMs in Forklift inventory after every clone completes.
# Sequential per-VM wait during cloning causes inventory sync failures on VM2+.
inventory_timeout = plan.get("inventory_timeout", 300)
wait_for_cloned_vms_in_forklift_inventory(
    source_provider=source_provider,
    source_provider_inventory=source_provider_inventory,
    cloned_vm_names=cloned_vm_names,
    inventory_timeout=inventory_timeout,
)
```

Two extra waits sit next to that call:

- When any VM uses `add_nic`, `wait_for_added_nics_in_forklift_inventory()` forces a provider refresh and blocks until inventory shows the new NIC count (`conftest.py:1379`,
  `utilities/provider_inventory.py:44`).
- On vSphere, `wait_for_cloned_vms_in_forklift_inventory()` additionally waits for inventory MAC addresses to converge with live vCenter MACs, because vSphere clones regenerate
  MACs (`utilities/provider_inventory.py:176`).

> **Note:** A map-generation failure often means Forklift inventory has not finished syncing the VM yet. The suite intentionally waits for inventory before creating maps, because
> MTV consumes inventory objects, not the provider SDK's in-memory objects.

## Provider Configuration And Selection

The selection logic is intentionally simple: `.providers.json` is loaded, and `py_config["source_provider"]` picks one named entry. Each entry's `type` field is then compared
against `Provider.ProviderType` by small predicate helpers in `utilities/utils.py`: `ocp_provider()`, `vmware_provider()`, `rhv_provider()`, `openstack_provider()`,
`hyperv_provider()`, and `ova_provider()` (`utilities/utils.py:121-148`).

```python
requested_provider = py_config["source_provider"]
if requested_provider not in source_providers:
    raise ValueError(
        f"Source provider '{requested_provider}' not found in '{providers_path}'. "
        f"Available providers: {sorted(source_providers.keys())}"
    )

_source_provider = source_providers[requested_provider]
fixture_store["source_provider_data"] = _source_provider
return _source_provider
```

`providers_schema.json` is the machine-readable contract for every provider entry. It defines six `oneOf` provider definitions plus `guestCredentials` and `copyoffloadConfig`
helper definitions.

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

| Field | Why the suite reads it |
| --- | --- |
| `type` | Selects the provider class, the secret keys, and the Forklift inventory adapter |
| `version` | Used in generated resource names by `base_resource_name` |
| `api_url` | Written into the source provider secret and the `Provider` CR |
| `username`, `password` | Read before provider-specific branching |
| `fqdn` | Used by the CA certificate helper, and as the SDK `host` for vSphere and Hyper-V |
| `storage_class` | Destination default for `get_storage_migration_map()`, OpenShift entries only |

The `Provider` CR always receives `vddk_init_image` and `sdk_endpoint` from the optional `vddk_init_image` and `endpoint_type` keys (`utilities/utils.py:544`).

### Per-Provider Configuration Fields

Required fields come from `providers_schema.json`; the "extra fields read by code" column comes from `create_source_provider()`.

| `type` | Required by schema | Extra fields read by code |
| --- | --- | --- |
| `vsphere` | `version`, `fqdn`, `api_url`, `username`, `password` | `endpoint_type`, `vddk_init_image`, `clone_provider`, `luks_passphrase`, `copyoffload` |
| `ovirt` | `version`, `fqdn`, `api_url`, `username`, `password` | none; `ca_file` or `insecure` is derived instead |
| `openstack` | the common six plus `user_domain_name`, `region_name`, `project_name`, both domain IDs | none |
| `openshift` | `version`, `host`, `api_url`, `username`, `password`, `storage_class` | `verify_ssl`, `ca_bundle`; no `fqdn` in this definition |
| `ova` | `version`, `fqdn`, `api_url`, `username`, `password` | none |
| `hyperv` | `version`, `fqdn`, `api_url`, `username`, `password`, `smb_url` | `smb_user`, `smb_password` |

Provider-specific secret keys and SDK arguments are then added on top (`utilities/utils.py:426-511`):

| Source type | Secret `stringData` keys added | SDK arguments added |
| --- | --- | --- |
| `vsphere` | `user`, `password`, `cacert` | `host=fqdn`, `copyoffload` |
| `ovirt` | `user`, `password`, `cacert` (always fetched) | `host=api_url`, plus `ca_file` when secure or `insecure` when not |
| `openstack` | `username`, `password`, `regionName`, `projectName`, `domainName`, `cacert` | `host`, `auth_url`, `project_name`, `user_domain_name`, `region_name`, domain IDs |
| `openshift` | none; the destination cluster secret is reused | none; `api_url` is replaced by the live cluster host |
| `ova` | none beyond `url` and `insecureSkipVerify` | `host=api_url` |
| `hyperv` | `username`, `password`, `smbUrl`, `smbUser`, `smbPassword`, `cacert` | `host=fqdn`, `cert_validation` set to the cert path or `False` |

> **Warning:** `create_source_provider()` reads `api_url`, `username`, and `password` before provider-specific branching, so every provider entry needs those keys. `version` is
> also needed for generated resource names. When certificate download is involved, `fqdn` matters too, because the certificate helper always connects to `<fqdn>:<port>`
> (`utilities/utils.py:153`).

> **Note:** Hyper-V fetches its CA certificate on port **5986**, not the default 443 used by every other provider (`utilities/utils.py:503`).

> **Note:** `create_source_provider()` waits up to 600 seconds for the `Provider` CR to reach `READY`. OVA sources pass `stop_status=None` so a transient NFS `ConnectionFailed` is
> retried instead of aborting the run (`utilities/utils.py:550`).

## The BaseProvider Contract

`BaseProvider` is the common surface that keeps the rest of the suite provider-agnostic. Each concrete provider can use its own SDK, but it must expose the same core operations to
the test code.

The normalized VM shape is defined in `libs/base_provider.py:18`:

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
    "firmware": {},
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

    Args:
        names: List of VM or template names to query
        inventory: Forklift inventory instance (required for all providers)

    Returns:
        List of network mappings in format [{"name": "network1"}, ...]
    """
```

In practice, `BaseProvider` gives the suite six important guarantees:

- `connect()` and `disconnect()` manage the direct SDK session.
- `test` is used immediately after provider creation to fail fast when the source is not reachable (`utilities/utils.py:555`).
- `vm_dict()` returns a normalized view of the source or destination VM.
- `clone_vm()` and `delete_vm()` let the suite prepare and clean up source-side test VMs without special-case code in every test.
- `get_vm_or_template_networks()` lets the suite size destination networking even before cloned VMs have finished syncing into inventory.
- `supports_skip_clone()` declares whether plan-readiness tests may use real VMs instead of clones. It defaults to `True`, and only `OvirtProvider` overrides it to `False` because
  RHV plan names are templates (`libs/base_provider.py:126`, `libs/providers/rhv.py:381`).

> **Warning:** `skip_clone=True` is incompatible with `disable_drs_for_vms`, `clone_to_same_host`, `migrate_shared_disks`, `add_nic`, and — outside Hyper-V — `preserve_static_ips`,
> because all of those need the cloning phase (`conftest.py:1194`).

## Provider-Specific Behavior

### vSphere

`VMWareProvider` in `libs/providers/vmware.py` is the most feature-rich source implementation in the repo.

- It uses `pyVmomi` for clone, power, disk, and guest-information operations.
- Clone-time options cover disk provisioning (`thin`, `thick-lazy`, `thick-eager`), extra disks, datastore overrides, ESXi host overrides, MAC regeneration, RDM disks, and Change
  Block Tracking for warm migrations (`libs/providers/vmware.py:80`, `libs/providers/vmware.py:1602`).
- `get_vm_or_template_networks()` queries Forklift inventory first and falls back to a direct vCenter query when the name is a template that inventory does not know yet
  (`libs/providers/vmware.py:2046`).
- If `copyoffload.esxi_clone_method` is set to `ssh`, `update_provider_clone_method()` patches the MTV `Provider` CR to set `spec.settings.esxiCloneMethod` and waits up to 180
  seconds for `Validated=True` (`libs/providers/vmware.py:109`).
- When copy-offload is configured, the source `Provider` CR also gets the annotation `forklift.konveyor.io/empty-vddk-init-image: yes` (`utilities/utils.py:533`).
- Standard network and storage mappings come from `VsphereForkliftInventory`. Copy-offload storage mappings can bypass inventory and use explicit datastore IDs instead.
- It is the only provider that implements shared-disk handling: `relink_shared_disks()`, `find_shared_vmdk_paths()`, and `reattach_orphaned_vmdk()`. `prepared_plan` refuses
  `migrate_shared_disks` for any other provider type (`conftest.py:1145`).

> **Note:** The test-side vSphere SDK connection uses `disableSslCertValidation=True`, while the Forklift `Provider` CR still honors `source_provider_insecure_skip_verify` and can
> include `cacert`. That means direct provider access and MTV-side validation are related, but not identical, code paths.

### RHV / oVirt

`OvirtProvider` in `libs/providers/rhv.py` has three important behaviors that are easy to miss.

- It refuses to connect unless the RHV datacenter named `MTV-CNV` exists and its status is `up` (`libs/providers/rhv.py:268`).
- `clone_vm()` clones from a template, not from a running VM. In other words, `source_vm_name` is treated as a template name in RHV flows, and `supports_skip_clone()` returns
  `False`.
- `get_vm_or_template_networks()` ignores inventory during pre-clone network discovery and queries template NICs directly through `get_template_networks()`.
- Later, once cloned VMs exist and Forklift has synced them, final `NetworkMap` and `StorageMap` generation still comes from inventory.
- RHV always fetches a CA certificate in `create_source_provider()`, even when insecure mode is enabled, because the code comments call out imageio as a dependency for that
  certificate (`utilities/utils.py:459`).

### OpenStack

`OpenStackProvider` in `libs/providers/openstack.py` clones more like an image pipeline than a simple VM copy.

- It snapshots the source server with `create_server_image()`.
- It creates new volumes from the resulting snapshots.
- It preserves boot order across the recreated volumes.
- It boots the cloned server from those new volumes.

OpenStack is also the strictest inventory-sync path. The suite does not treat "VM exists in inventory" as enough. `ForkliftInventory.wait_for_vm()` also waits for attached volumes
and networks to become queryable (`libs/forklift_inventory.py:230`):

```python
# For OpenStack, verify volumes and networks are synced
if self.provider_type == Provider.ProviderType.OPENSTACK:
    if not (self._check_openstack_volumes_synced(vm, name) and self._check_openstack_networks_synced(vm, name)):
        return None
```

That matters because OpenStack storage mapping is based on volume type, and network mapping is based on inventory network objects matched against VM address data. If either side is
late, map generation will be wrong.

### Hyper-V

`HyperVProvider` in `libs/providers/hyperv.py` drives Hyper-V over PowerShell Remoting Protocol using `pypsrp`, not over a vendor SDK.

- `connect()` builds a `WSMan` session on port 5986 with NTLM auth plus an opened `RunspacePool`; `disconnect()` tears both down (`libs/providers/hyperv.py:364`).
- VM queries return `pypsrp.complex_objects.GenericComplexObject` values rather than typed SDK models, and most data is read from embedded PowerShell scripts
  (`_NIC_DETAILS_SCRIPT`, `_GUEST_OS_NAME_SCRIPT`).
- `get_vm_or_template_networks()` delegates straight to `inventory.vms_networks_mappings()` (`libs/providers/hyperv.py:990`).
- It has `wait_for_guest_network_config()` for guest network readiness, which `prepared_plan` uses in place of the vSphere `wait_for_vmware_guest_info()` when `source_vm_power` is
  `on` (`conftest.py:1293`).
- `skip_clone=True` is allowed for Hyper-V, and `preserve_static_ips` stays compatible with it because guest tools are reachable without cloning (`conftest.py:1196`).
- Windows detection uses the guest `OSName`/`Notes` KVP token `windows`, not the VM name, so names such as `twin-server` are classified correctly (`libs/providers/hyperv.py:29`,
  `libs/providers/hyperv.py:737`).

### OpenShift

`OCPProvider` is both the destination provider for migrations and a supported source provider for source-side CNV test setups.

- If OpenShift is the source, `prepared_plan` creates a source `NetworkAttachmentDefinition` plus source CNV VMs through `create_source_cnv_vms()` (`conftest.py:1122`).
- `OpenshiftForkliftInventory` resolves storage by following the VM's data volumes to PVCs and then reading `storageClassName` from the live cluster.
- It resolves networks from the VM template: `multus.networkName` becomes a named source network, and a pod network becomes `{"type": "pod"}`.
- `vm_dict()` waits for the CNV guest agent for up to 301 seconds and sanitizes VM names to Kubernetes-safe resource names before querying the cluster
  (`libs/providers/openshift.py:155`).
- It exposes `create_ssh_connection_to_vm()`, which builds an `rrmngmnt` SSH connection against the destination VM (`libs/providers/openshift.py:392`).

### OVA

`OVAProvider` is intentionally thin.

- `connect()` returns `self` and `test` is always `True`; `disconnect()` only logs.
- `clone_vm()` and `delete_vm()` are no-ops. OVA sources are never cloned by `prepared_plan`.
- `vm_dict()` fills the normalized template with the provider type, `power_state="off"`, and `win_os=False`, then upgrades `win_os` from Forklift inventory's OVF `osType` when an
  inventory instance is passed (`libs/providers/ova.py:34`).
- `get_vm_or_template_networks()` delegates to `inventory.vms_networks_mappings()`.
- In the current `prepared_plan` implementation, the source VM name stays unchanged; instead each VM gets a unique `targetName` derived from
  `sanitize_kubernetes_name(f"{session_uuid}-{vm['name']}")` so parallel sessions do not collide (`conftest.py:1402`).
- In practice, OVA mapping behavior depends much more on Forklift inventory than on direct provider logic.

> **Note:** Warm-migration coverage is marker-scoped rather than skipped in code. `tests/warm/test_mtv_warm_migration.py` carries `@pytest.mark.vsphere` and `@pytest.mark.rhv`, and
> `tests/warm/test_warm_migration_comprehensive.py` carries the same two. No OpenStack, OpenShift, or OVA warm tests exist.

## Forklift Inventory Adapters

Forklift inventory lives behind the `forklift-inventory` `Route` in the MTV namespace. Each adapter subclasses `ForkliftInventory` and knows how to translate that provider's
inventory model into the source-side names and IDs that MTV map CRs expect.

The adapter selection is a registry in `libs/forklift_inventory.py`, not a literal dict in `conftest.py`:

```python
PROVIDER_INVENTORY_MAP: dict[str, type[ForkliftInventory]] = {}


def _register_inventory_classes() -> None:
    """Populate PROVIDER_INVENTORY_MAP after all classes are defined."""
    PROVIDER_INVENTORY_MAP.update({
        Provider.ProviderType.OVA: OvaForkliftInventory,
        Provider.ProviderType.RHV: OvirtForkliftInventory,
        Provider.ProviderType.VSPHERE: VsphereForkliftInventory,
        Provider.ProviderType.OPENSHIFT: OpenshiftForkliftInventory,
        Provider.ProviderType.OPENSTACK: OpenstackForliftinventory,
        Provider.ProviderType.HYPERV: HypervForkliftInventory,
    })
```

`create_forklift_inventory()` registers lazily on first use, raises `ValueError` when `provider.ocp_resource` is unset, and raises `ValueError` for an unregistered provider type.

The adapters all use the same base route plumbing in `libs/forklift_inventory.py`: they discover the provider ID, build a provider-specific URL path, and then query VM, network,
and storage endpoints through the inventory service. Provider ID discovery retries for up to 180 seconds with a 5 second sleep (`libs/forklift_inventory.py:93`).

| Source type | Adapter | Storage resolution | Network resolution |
| --- | --- | --- | --- |
| vSphere | `VsphereForkliftInventory` | Each disk's `datastore.id` matched to a datastore name | Each VM network `id` matched to a network name |
| RHV | `OvirtForkliftInventory` | Disk attachment -> `/disks/<id>` -> `storageDomain` -> domain name | NIC profile -> `/nicprofiles` -> network ID -> inventory `path` |
| OpenStack | `OpenstackForliftinventory` | Attached volume -> `/volumes/<id>` -> `volumeType`, plus `glance` | VM `addresses` keys matched to inventory network names |
| Hyper-V | `HypervForkliftInventory` | Each disk's `datastore.id` matched to a storage name | Each NIC's `network.id` matched to a network name |
| OpenShift | `OpenshiftForkliftInventory` | VM data volumes -> PVC -> live `storageClassName` | Template networks become named multus networks or `{"type": "pod"}` |
| OVA | `OvaForkliftInventory` | Storage entries whose name contains the VM name; returns storage `id` | VM network `ID` matched to an inventory network name |

Three adapter details are especially practical:

- RHV uses provider-side template queries for pre-clone network discovery, but inventory for final map generation.
- OpenShift storage resolution is not just "whatever inventory says." The adapter actually follows data volumes to PVCs and reads the live `storageClassName`.
- Only `VsphereForkliftInventory` exposes a `hosts` property, which is what the copy-offload host-concurrency tests use (`libs/forklift_inventory.py:438`).

> **Warning:** `vms_networks_mappings()` deduplicates by default. Passing `deduplicate=False` returns one entry per NIC, which is what the `per_nic_network_map` plan option uses
> through `get_per_nic_networks()` (`libs/forklift_inventory.py:284`, `utilities/utils.py:257`).

## How Source Network Mappings Are Resolved

Network mapping is a two-step process.

First, the suite decides how many destination networks it needs. It does that in the `multus_network_name` fixture by calling `source_provider.get_vm_or_template_networks()`
(`conftest.py:869`). It creates `len(networks) - 1` NADs named `{base}-{i}`, where `base` is `cb-<hash>` derived from the node ID and `session_uuid` (`conftest.py:888`,
`conftest.py:893`). This is why RHV can use template networks before clones exist.

Second, once the cloned or prepared source VM has been synced into Forklift inventory, the actual `NetworkMap` payload is built from inventory data.

The core rule lives in `utilities/utils.py`:

```python
if per_nic_network_map:
    networks = get_per_nic_networks(
        source_provider=source_provider, source_provider_inventory=source_provider_inventory, vms=vms
    )
else:
    networks = source_provider_inventory.vms_networks_mappings(vms=vms)

for index, network in enumerate(networks):
    if pod_only or index == 0:
        # First network or pod_only mode → pod network
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
4. If the plan config sets `multus_namespace`, the NADs are created there instead of in the main target namespace (`conftest.py:851`).

> **Warning:** Network mapping is order-based. The first source network returned by inventory becomes the pod network, so inventory ordering matters for multi-NIC VMs.

> **Tip:** If a source VM has `N` networks, the suite creates `N - 1` NADs, because the first network is reserved for the destination pod network. VMs configured with `add_nic` add
> one more NAD each (`conftest.py:875`).

## How Source Storage Mappings Are Resolved

The storage path is simpler than the network path in standard migrations: the helper trusts the selected inventory adapter to tell it which source storage objects matter, then it
adds the destination storage class.

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

- The destination storage class is `py_config["storage_class"]` unless the caller passes an explicit `storage_class` argument (`utilities/mtv_migration.py:608`).
- The meaning of the source side is provider-specific. On vSphere it is a datastore. On RHV it is a storage domain. On OpenStack it is a volume type or `glance`. On Hyper-V it is a
  storage name. On OpenShift it is a storage class. On OVA it is a storage ID.
- If the adapter returns the wrong source identifier, the `StorageMap` will be wrong even if the direct provider SDK can see the disks just fine.

### vSphere Copy-Offload

Copy-offload is the main exception to inventory-derived storage mapping. In copy-offload mode, `get_storage_migration_map()` can build the source side from explicit datastore IDs
instead of asking inventory.

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

`non_xcopy_datastore_id` is handled as its own branch after the XCOPY loop: it is still mapped, still with the offload plugin attached, so Forklift can fall back to non-XCOPY
transfer.

The repo validates copy-offload prerequisites in `tests/copyoffload/conftest.py`:

```python
required_credentials = ["storage_hostname", "storage_username", "storage_password"]
required_params = ["storage_vendor_product", "datastore_id"]
```

In practice, the copy-offload flow expects:

- `storage_vendor_product`, one of the nine values in `SUPPORTED_VENDORS`
- `datastore_id`
- `storage_hostname`
- `storage_username`
- `storage_password`

Optional extensions used by the code include:

- `secondary_datastore_id`
- `non_xcopy_datastore_id`
- `esxi_clone_method`
- `esxi_host`, `esxi_user`, `esxi_password`
- `default_vm_name`, `resource_pool`, `dedicated_migration_hosts`, `rdm_lun_uuid`

The copy-offload storage `Secret` is built in `tests/copyoffload/conftest.py:402` with the base keys `STORAGE_HOSTNAME`, `STORAGE_USERNAME`, and `STORAGE_PASSWORD`, plus a
vendor-specific table:

| `storage_vendor_product` | Additional secret keys |
| --- | --- |
| `ontap` | `ONTAP_SVM` from `ontap_svm` |
| `vantara` | `STORAGE_ID`, `STORAGE_PORT`, `HOSTGROUP_ID_LIST` from the three `vantara_*` keys |
| `primera3par`, `powerstore`, `infinibox`, `flashsystem` | none beyond the base credentials |
| `pureFlashArray` | `PURE_CLUSTER_PREFIX` from `pure_cluster_prefix` |
| `powerflex` | `POWERFLEX_SYSTEM_ID` from `powerflex_system_id` |
| `powermax` | `POWERMAX_SYMMETRIX_ID` from `powermax_symmetrix_id` |

Arbitrary vendor keys go through `storage_secret_extra` in `.providers.json` or the `COPYOFFLOAD_STORAGE_SECRET_EXTRA` environment variable, which is merged last by
`merge_storage_secret_extra()` (`utilities/copyoffload_migration.py:225`).

The test suite builds the offload plugin config like this:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}
```

When `copyoffload=True`, `create_plan_resource()` also forces `pvc_name_template` to `"pvc"` because the volume-populator path expects predictable PVC naming
(`utilities/mtv_migration.py:329`).

> **Warning:** Copy-offload in this repository is a vSphere-specific path. The `copyoffload_config` fixture explicitly raises `ValueError` if the selected source provider is not
> vSphere (`tests/copyoffload/conftest.py:76`).

> **Tip:** Copy-offload credentials can come from environment variables as well as `.providers.json`. `get_copyoffload_credential()` checks `COPYOFFLOAD_{FIELD_UPPER}` first and
> only then the config file, so environment variables win (`utilities/copyoffload_migration.py:98`).

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

Second, it can supply disk filenames for PVC-template validation. In `utilities/post_migration.py`, the `{{.FileName}}` wildcard is resolved from Forklift inventory disk file
paths, not from the direct provider SDK. That validation path is only enabled for vSphere sources, and it raises `ValueError` for any other provider type
(`utilities/post_migration.py:1083`).

> **Tip:** If you use `pvc_name_template` with `{{.FileName}}`, you are depending on inventory disk metadata. The suite sorts vSphere source disks by `(controller_key,
> unit_number)` before it renders expected PVC names.

> **Note:** `pvc_name_template` accepts either a string or a mapping keyed by `Provider.ProviderType` values plus `"default"`. `resolve_pvc_name_template()` rejects unknown keys
> and falls back to `"default"` when the source provider has no entry (`utilities/mtv_migration.py:131`).

## Real Test Config Examples

These are exact snippets from `tests/tests_config/config.py`. They are useful because they show the real shapes the repository already exercises.

### Comprehensive Warm Migration Example

This example combines custom VM namespace placement, cross-namespace Multus, static IP preservation, and a per-provider PVC naming template.

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
    "enable_nested_virtualization": False,
    "vm_target_namespace": f"mtv-vms-warm-comprehensive-{uuid.uuid4().hex[:4]}",
    "multus_namespace": "default",  # Cross-namespace NAD access
    # Keys must be Provider.ProviderType string values (e.g. "vsphere") or "default".
    # vsphere uses .FileName; default truncates VmName to stay within 63 chars.
    "pvc_name_template": {
        "vsphere": '{{ .FileName | trimSuffix ".vmdk" | replace "_" "-" }}-{{.DiskIndex}}',
        "default": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
    },
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
    "guest_agent_timeout": 600,
},
```

### Mixed-Datastore Copy-Offload Example

This example shows a vSphere copy-offload plan where an added disk is intentionally placed on a non-XCOPY datastore.

```python
"test_copyoffload_mixed_datastore_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
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

> **Warning:** Symbolic datastore values such as `secondary_datastore_id` and `non_xcopy_datastore_id` are not global magic strings. `resolve_datastore_moid_from_disk_config()`
> looks them up in the selected provider's `copyoffload` section and raises a clear error when the key is not configured (`utilities/copyoffload_datastore.py:53`).

Once you know which data comes from the direct provider class and which data comes from Forklift inventory, the rest of the repository becomes much easier to predict. Provider
classes explain how source VMs are created and inspected. Inventory adapters explain how MTV sees those VMs. The map helpers then turn that inventory view into the exact
`StorageMap`, `NetworkMap`, and `Plan` payloads that the migration uses.
