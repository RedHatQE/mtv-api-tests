# Plan And Resource Reference

The suite builds the same MTV resource chain you would create by hand: `Provider`, `StorageMap`, `NetworkMap`, `Plan`, and `Migration`. For specific scenarios it also creates
`Hook`, `Secret`, `Namespace`, `Conversion`, and `NetworkAttachmentDefinition` resources so the plan can run end to end.

> **Note:** The dictionaries in `tests/tests_config/config.py` are not raw CR YAML. Some keys become CR fields, while others only control setup or validation. For example, `clone`,
> `guest_agent`, and `source_vm_power` affect fixture behavior, not the final `Plan` spec.

Every CR shown below is rendered by `openshift-python-wrapper`, so the YAML field names are the ones the cluster actually receives.

## Lifecycle Overview

| Resource | Why the suite creates it | Default placement | Extra readiness rule |
| --- | --- | --- | --- |
| `Namespace` | Isolate each run and any optional VM/NAD namespaces | Per-run `target_namespace`, plus optional custom namespaces | Waits for `Active` |
| `Secret` | Provider credentials, OCP token, copy-offload credentials | Per-run `target_namespace`; AAP token secret in `mtv_namespace` | Created with deploy wait only |
| `Provider` | Source and destination endpoints for MTV | Per-run `target_namespace` | Waits for `Ready` within 600s; SSH copy-offload also waits for `Validated=True` |
| `StorageMap` | Map source storage to the destination storage class or offload plugin | Per-run `target_namespace` | Created with deploy wait only |
| `NetworkMap` | Map source NICs to pod or Multus networks | Per-run `target_namespace` | Created with deploy wait only |
| `Hook` | Run pre- or post-migration Ansible playbooks, or an AWX job template | Per-run `target_namespace` | Config is validated before creation |
| `Plan` | Tie providers, mappings, VMs, and optional plan settings together | Per-run `target_namespace` | Waits for `Plan` condition `Ready=True` within 360s |
| `Migration` | Execute a `Plan` | Per-run `target_namespace` | The suite watches the `Plan` until it reaches `Succeeded` or `Failed` |
| `Conversion` | Deep Inspection (standalone or plan-driven) conversion jobs | Per-run `target_namespace` | Polls `status.phase` until `Succeeded`, `Failed`, or `Canceled` |
| `ForkliftController` (patch only) | Precopy interval, and AAP hook settings | `mtv_namespace` | Waits for `Running=True`, then `Successful=True`, within 300s per wait |

## Where Resources Live

| Namespace | What the suite puts there |
| --- | --- |
| `target_namespace` | `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Migration`, `Hook`, `Conversion`, provider `Secret`s, and copy-offload storage `Secret`s |
| `vm_target_namespace` | Migrated VMs only, when you set `vm_target_namespace` |
| `multus_namespace` | Extra `NetworkAttachmentDefinition`s, when you set `multus_namespace` |
| `${session_uuid}-source-vms` | Temporary source CNV VMs for OpenShift source-provider tests |
| `mtv_namespace` | The AWX/AAP token `Secret` created for `aap` hook tests; operator objects such as `ForkliftController`, the `Subscription`, and the inventory route |

> **Note:** The test-owned migration CRs are created in the per-run `target_namespace`. `mtv_namespace` defaults to `openshift-mtv`; the suite only reads it, except for the
> AAP patch.

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

- Auto-generated names start from `base_resource_name`, which is built as `{session_uuid}-source-{provider_type}-{version_with_dashes}`.
- Copy-offload source providers add `-xcopy` to that base name.
- Auto-generated `Plan` and `Migration` names also add `-warm` or `-cold`.
- If a name is longer than 63 characters, the helper keeps the last 63 characters so the unique suffix survives.
- If you pass an explicit `name`, or a `kind_dict`/`yaml_file` that already contains one, that name wins.
- Every created resource is tracked for teardown under its kind, and an optional `test_name` is stored with the entry.

The suite also applies related naming rules outside CR creation:

- `session_uuid` comes from `generate_name_with_uuid("auto")`, so generated run identifiers look like `auto-xxxx`.
- `target_namespace_prefix` defaults to `auto`; the fixture strips the literal `auto` before appending it to `session_uuid` so default runs do not produce doubled prefixes.
- Destination VM lookups on OpenShift are sanitized to DNS-1123 format, so names with uppercase letters, `_`, or `.` are converted before lookup.
- Per-class resource names use a short hash: `sha256(f"{nodeid}\0{session_uuid}")[:6]`, rendered as `cb-<6-char-hash>`.

> **Tip:** When you are debugging a run, search by the session UUID first. Even after truncation, the suite keeps the unique suffix rather than the human-friendly prefix.

## Provider

Source provider definitions come from the repo-root `.providers.json`. The loader in `utilities/utils.py` reads that file, and the `source_provider` pytest config value chooses one
top-level entry by name.

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

The example file supports these provider types, and those strings are exactly the values that reach `spec.type`:

| `.providers.json` key | `Provider.ProviderType` | `spec.type` value used in the CR |
| --- | --- | --- |
| `vsphere` | `VSPHERE` | `vsphere` |
| `ovirt` | `RHV` | `ovirt` |
| `openstack` | `OPENSTACK` | `openstack` |
| `openshift` | `OPENSHIFT` | `openshift` |
| `ova` | `OVA` | `ova` |
| `hyperv` | `HYPERV` | `hyperv` |

The file is validated hard, so a malformed template fails immediately:

> **Warning:** The suite fails early if the providers file is missing, empty, not a JSON mapping, or if `source_provider` does not match a top-level key exactly. The shipped
> `.providers.json.example` also needs its inline comments removed before you can use it as a real `.providers.json`.

### Provider CR shape

`create_source_provider()` creates a `Secret` first, then the `Provider` that references it. The rendered CR looks like this:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: Provider
metadata:
  name: auto-abc123-source-vsphere-8-0-3
  namespace: auto-abc123
  annotations:
    forklift.konveyor.io/empty-vddk-init-image: "yes"   # only for vSphere copy-offload providers
spec:
  type: vsphere
  url: https://vcenter.example.com/sdk
  secret:
    name: auto-abc123-source-vsphere-8-0-3-abc
    namespace: auto-abc123
  settings:
    vddkInitImage: /var/run/vddk      # from vddk_init_image
    sdkEndpoint: vcenter              # from endpoint_type
```

| `spec` field | Source in the suite |
| --- | --- |
| `type` | The provider `type` from `.providers.json` |
| `url` | `api_url`; rewritten to the cluster host when the source provider is OpenShift |
| `secret.name`, `secret.namespace` | The test-created `Secret` in the same namespace |
| `settings.vddkInitImage` | `vddk_init_image` when present |
| `settings.sdkEndpoint` | `endpoint_type` when present |
| `metadata.annotations[forklift.konveyor.io/empty-vddk-init-image]` | Set to `"yes"` for vSphere providers that declare a `copyoffload` section |

Secret keys per provider type, all under `string_data`:

| Provider type | Secret keys |
| --- | --- |
| All | `url`, `insecureSkipVerify` |
| `vsphere`, `ovirt` | `user`, `password`, plus the CA certificate when SSL verification is on |
| `openstack` | `username`, `password`, `regionName`, `projectName`, `domainName` |
| `hyperv` | `username`, `password`, `smbUrl`, and `smbUser` / `smbPassword` when configured |
| `openshift` | No new secret; the destination OCP token secret is reused |

The CA certificate is stored under the `cacert` key by default. The `ca_crt` tests pass `ca_cert_key="ca.crt"` instead and assert that Forklift accepts the standard Kubernetes
field name (`tests/cold/conftest.py:18`, `tests/cold/test_ca_crt_migration.py:76`).

### Destination providers

| Case | How it is created |
| --- | --- |
| Local destination | `kind_dict` `Provider` `${session_uuid}-local-ocp-provider`, `spec.type: openshift`, empty `spec.secret` and `spec.url` (`conftest.py:711`) |
| Remote destination | `Secret` with `token` and `insecureSkipVerify`, then `Provider` `${session_uuid}-destination-ocp-provider` of type `openshift` (`conftest.py:986`) |

### Provider readiness rules

Provider readiness is stricter than for most other resources:

- The source `Provider` must reach `Ready` within 600 seconds: `wait_for_status(Provider.Status.READY, timeout=600, stop_status="ConnectionFailed")`.
- OVA is the exception: NFS-backed OVA providers can report `ConnectionFailed` transiently, so `stop_status` is `None` for them and the suite retries until timeout.
- After the `Provider` exists, Forklift inventory must expose it before the suite can build storage and network mappings.
- For VMware copy-offload with `esxi_clone_method: "ssh"`, `libs/providers/vmware.py` patches `spec.settings.esxiCloneMethod: ssh` and then waits for `Validated=True` with a
  180-second timeout.

## Mapping Source Identifiers

The suite does not hard-code one universal `source` shape for mappings. Instead, it asks provider-specific inventory adapters in `libs/forklift_inventory.py` for the right source
identifiers.

| Source provider type | `StorageMap` source shape | `NetworkMap` source shape |
| --- | --- | --- |
| `vsphere` | datastore `name` | network `name` |
| `ovirt` | storage domain `name` | network `name` (resolved from the NIC profile’s network `path`) |
| `openstack` | volume type `name`, plus the synthetic `glance` entry for image-backed VMs | network `id` and `name` |
| `openshift` | storage class `name` | `{"type": "pod"}` or the Multus `networkName` |
| `ova` | storage `id` | network `name` |
| `hyperv` | storage `name` | network `id` and `name` |

Every adapter fails fast rather than producing an empty map:

> **Note:** An adapter raises `ValueError` when no storage or no network is found for the requested VMs.

One provider needs extra waiting before that is even possible:

> **Note:** OpenStack inventory is treated a little more carefully than the others. The suite waits not only for the VM to appear in Forklift inventory, but also for its attached
> volumes and networks to become queryable before it builds mappings.

## StorageMap

A standard `StorageMap` is inventory-driven: the suite asks Forklift which source storages the chosen VMs use, then maps each one to the destination `storage_class`.

> **Warning:** `storage_class` is not defined in `tests/tests_config/config.py`. The suite expects it from pytest config, and `get_storage_migration_map()` falls back to
> `py_config["storage_class"]`.

For copy-offload scenarios, the `StorageMap` carries an offload plugin configuration instead of relying only on source inventory.

From `tests/copyoffload/test_copyoffload_migration.py:119`:

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
    volume_mode="Block",
)
```

The rendered CR is always the same envelope, with only `spec.map` differing:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: StorageMap
metadata:
  name: auto-abc123-source-vsphere-8-0-3-xcopy-abc
  namespace: auto-abc123
spec:
  map:
    - destination:
        storageClass: my-block-storageclass
        accessMode: ReadWriteOnce
        volumeMode: Block
      source:
        id: datastore-11
      offloadPlugin:
        vsphereXcopyConfig:
          secretRef: auto-abc123-source-vsphere-8-0-3-xcopy-abc
          storageVendorProduct: vSAN
    - destination:
        storageClass: my-block-storageclass
      source:
        name: MyDatastore
  provider:
    source:
      name: auto-abc123-source-vsphere-8-0-3
      namespace: auto-abc123
    destination:
      name: auto-abc123-local-ocp-provider
      namespace: auto-abc123
```

In practice, that means:

- Standard mode maps each source storage to `{"destination": {"storageClass": <storage_class>}, "source": <inventory value>}`.
- Copy-offload mode maps datastores by `id` and adds `offloadPlugin`, plus `accessMode` / `volumeMode` when the caller passes them. The copy-offload tests pass
  `volume_mode="Block"` and leave `access_mode` unset, so `accessMode` only appears if a caller supplies it.
- Mixed and multi-datastore copy-offload are supported through `secondary_datastore_id` and `non_xcopy_datastore_id`. A non-XCOPY datastore still gets the
  `offloadPlugin` block so Forklift can fall back from XCOPY.

> **Warning:** `secondary_datastore_id` and `non_xcopy_datastore_id` are only valid when `datastore_id` is also set, and `datastore_id` requires `offload_plugin_config`. The
> helper raises `ValueError` for those combinations otherwise.

The copy-offload storage secret is also test-owned. Its credentials can come from environment variables or from the provider’s `copyoffload` section in `.providers.json`, and
vendor-specific keys are validated before the secret is created.

## NetworkMap

`NetworkMap` creation is intentionally simple and predictable:

- The first source network always maps to the pod network.
- Every additional source network maps to a Multus `NetworkAttachmentDefinition`.
- Extra NADs are named from a short class hash so parallel tests do not collide.

From `utilities/utils.py:303`:

```python
network_map_list: list[dict[str, dict[str, str]]] = []
_destination_pod: dict[str, str] = {"type": "pod"}
multus_counter = 1

for index, network in enumerate(networks):
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

The rendered CR:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: NetworkMap
metadata:
  name: auto-abc123-source-vsphere-8-0-3-abc
  namespace: auto-abc123
spec:
  map:
    - destination:
        type: pod
      source:
        name: VM Network
    - destination:
        name: cb-a1b2c3-1
        namespace: auto-abc123
        type: multus
      source:
        name: VM Network 2
  provider:
    source:
      name: auto-abc123-source-vsphere-8-0-3
      namespace: auto-abc123
    destination:
      name: auto-abc123-local-ocp-provider
      namespace: auto-abc123
```

That logic ties directly to the class-scoped NAD fixture in `conftest.py`:

- The base NAD name is `cb-<6-char-sha256>`.
- Additional NADs become `cb-<hash>-1`, `cb-<hash>-2`, and so on.
- The names are kept short to stay under Linux bridge interface limits.
- If you set `multus_namespace`, the suite creates or reuses that namespace and puts the NADs there. Otherwise they go into the main `target_namespace`.
- `per_nic_network_map=True` switches the source list from deduplicated inventory mappings to one entry per NIC (`get_per_nic_networks()`).

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

Hook configuration supports three mutually exclusive modes:

| Mode | Hook config key | Resulting `Hook` spec |
| --- | --- | --- |
| Predefined playbook | `expected_result`: `succeed` or `fail` | `image: quay.io/konveyor/hook-runner:latest` plus `playbook` |
| Custom playbook | `playbook_base64` | `image: quay.io/konveyor/hook-runner:latest` plus your base64 `playbook` |
| AAP / AWX job template | `aap_job_template_id` (positive integer) | `aap.jobTemplateId` only; no image |

```yaml
# predefined or custom playbook
apiVersion: forklift.konveyor.io/v1beta1
kind: Hook
metadata:
  name: auto-abc123-cold
  namespace: auto-abc123
spec:
  image: quay.io/konveyor/hook-runner:latest
  playbook: LS0tCi0gaG9zdHM6IGFsbAogIHRhc2tzOiBbXQo=
---
# AAP hook
apiVersion: forklift.konveyor.io/v1beta1
kind: Hook
metadata:
  name: auto-abc123-cold
  namespace: auto-abc123
spec:
  aap:
    jobTemplateId: 42
```

Validation rules are strict:

- `expected_result`, `playbook_base64`, and `aap_job_template_id` are mutually exclusive; you must set exactly one of them.
- Empty strings are rejected.
- Custom playbooks must be valid base64, valid UTF-8, valid YAML, and a non-empty Ansible play list.

For AAP hooks the suite also patches the operator, so Forklift can reach AWX. The patch is applied to `ForkliftController` in `mtv_namespace` and reverted afterwards:

```python
editor = ResourceEditor(
    patches={
        forklift_controller: {
            "spec": {
                "aap_url": awx_deployment,
                "aap_token_secret_name": token_secret_name,
                "aap_insecure_skip_verify": "true",
            }
        }
    }
)
editor.update(backup_resources=True)
```

`openshift-python-wrapper` applies a patch when the editor is entered as a context manager or when `update()` is called, so constructing the editor alone changes nothing. The fixture calls
`update(backup_resources=True)` and then `editor.restore()` in its teardown.

> **Note:** Hook failure steps are read from the `Plan` CR status, not from the `Migration`: `status.migration.vms[].pipeline[]`. A failing `PostHook` still leads to VM validation,
> while a failing `PreHook` skips VM checks because the migration never reached the VM validation stage.

## Plan

The `Plan` is where all of the pieces come together. Before the suite creates it, `prepared_plan` may clone VMs, adjust source power state, wait for cloned VMs to appear in
Forklift inventory, and call `populate_vm_ids()` so each VM entry has the inventory ID Forklift expects.

The most feature-rich plan-style config in the repo looks like this.

From `tests/tests_config/config.py`:

```python
"test_cold_migration_comprehensive": {
    "virtual_machines": [
        {
            "name": "mtv-win2019-3disks",
            "source_vm_power": "on",  # VM must be on for guest tools to report static IP info
            "guest_agent": True,
        },
    ],
    "warm_migration": False,
    "target_power_state": "on",
    "preserve_static_ips": True,
    "enable_nested_virtualization": False,
    "pvc_name_template": '{{ .VmName | trunc 32 | trimSuffix "-" }}-{{ .VmName | trunc -4 }}-disk-{{.DiskIndex}}',
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
    "vm_target_namespace": f"mtv-vms-cold-comprehensive-{uuid.uuid4().hex[:4]}",
    "multus_namespace": "default",  # Cross-namespace NAD access
},
```

### Plan spec fields the suite sets

| Config key (`tests/tests_config/config.py`) | `Plan` `spec` field | Notes |
| --- | --- | --- |
| `warm_migration` | `spec.warm` | Also drives the `-warm` / `-cold` name suffix |
| `vm_target_namespace` | `spec.targetNamespace` | Falls back to the run namespace when unset |
| `target_power_state` | `spec.targetPowerState` | `on` / `off` |
| `preserve_static_ips` | `spec.preserveStaticIPs` | |
| `pvc_name_template` | `spec.pvcNameTemplate` | Can be a string or a provider-keyed dict with a `default` fallback |
| `pvc_name_template_use_generate_name` | `spec.pvcNameTemplateUseGenerateName` | |
| `target_node_selector` | `spec.targetNodeSelector` | `None` values are auto-generated per session |
| `target_labels` | `spec.targetLabels` | `None` values are auto-generated per session |
| `target_affinity` | `spec.targetAffinity` | Passed through as-is |
| `migrate_shared_disks` | `spec.migrateSharedDisks` | Plan-level default for all VMs |
| `enable_nested_virtualization` | `spec.enableNestedVirtualization` | |
| `xfs_compatibility` | `spec.xfsCompatibility` | XFS v4 virt-v2v image |
| `run_preflight_inspection` | `spec.runPreflightInspection` | Warm Deep Inspection gate |
| `rdm_as_lun` | `spec.rdmAsLun` | vSphere only; RDM disks as `lun.bus: scsi` |
| `copyoffload` | `spec.pvcNameTemplate: "pvc"` | The volume populator framework requires this fixed template |
| `pre_hook` / `post_hook` | `spec.vms[].hooks[]` | Rendered as `{"hook": {"name", "namespace"}, "step": "PreHook" or "PostHook"}` |
| `multus_namespace` | not a `Plan` field | Tells the NAD fixture where to create extra Multus networks |

The `Plan` spec always contains the provider, map, and VM references:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: Plan
metadata:
  name: auto-abc123-source-vsphere-8-0-3-abc-cold
  namespace: auto-abc123
spec:
  warm: false
  targetNamespace: mtv-vms-cold-comprehensive-ab12
  map:
    storage:
      name: auto-abc123-source-vsphere-8-0-3-abc
      namespace: auto-abc123
    network:
      name: auto-abc123-source-vsphere-8-0-3-abc
      namespace: auto-abc123
  provider:
    source:
      name: auto-abc123-source-vsphere-8-0-3
      namespace: auto-abc123
    destination:
      name: auto-abc123-local-ocp-provider
      namespace: auto-abc123
  vms:
    - id: vm-101
      name: mtv-win2019-3disks
      migrateSharedDisks: true
      hooks:
        - hook:
            name: auto-abc123-cold
            namespace: auto-abc123
          step: PreHook
  pvcNameTemplateUseGenerateName: false
  pvcNameTemplate: "{{.VmName}}-disk-{{.DiskIndex}}"
  targetPowerState: "on"
  xfsCompatibility: true
  migrateSharedDisks: true
  preserveStaticIPs: true
  targetNodeSelector:
    mtv-comprehensive-node: auto-abc123
  targetLabels:
    mtv-comprehensive-label: auto-abc123
  targetAffinity:
    podAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - podAffinityTerm:
            labelSelector:
              matchLabels:
                app: test
            topologyKey: kubernetes.io/hostname
          weight: 50
  enableNestedVirtualization: false
  runPreflightInspection: false
  rdmAsLun: true
```

Two VM-level details matter when reading `spec.vms`:

- Per-VM `migrate_shared_disks` in the config is renamed to the camelCase API field `migrateSharedDisks` before the CR is built.
- Config-only VM keys never reach the CR. `add_nic`, `add_nic_start_connected`, `connected_nic_mac`, `disconnected_nic_mac`, and `snapshots_before_migration` are stripped in
  `create_plan_resource()`.

`pvc_name_template` is especially useful when you want predictable PVC names:

- The validation helper supports `{{.VmName}}`, `{{.DiskIndex}}`, and VMware-only `{{.FileName}}`.
- It also supports Sprig functions, because validation uses a Go template renderer (`py-go-template`).
- `{{.FileName}}` is skipped with a warning unless the source provider is vSphere, since the filenames come from vSphere inventory.
- Long generated names are truncated during validation to match Kubernetes limits: the `generateName` prefix limit or the plain name limit, depending on the mode.
- When `pvc_name_template_use_generate_name` is `True`, the suite checks only the generated prefix because Kubernetes adds its own random suffix.

> **Tip:** Use `None` in `target_node_selector` and `target_labels` when you want uniqueness without inventing a new value yourself. The suite will replace it with the run’s
> `session_uuid`.

After creation, the plan has its own readiness rules:

- The suite waits for `Plan.Condition.READY` with status `True` and a 360-second timeout.
- On timeout, it logs the `Plan` plus both source and destination provider objects to make provider-side issues easier to debug.
- For copy-offload plans, it also waits up to 60 seconds for Forklift to create a plan-specific secret. The match is either a secret whose name starts with
  `<plan-name>-`, or one labeled `plan-name=<plan>` together with `isPopulator`.

The migrated VMs and the `Plan` never share a namespace unless you ask for it:

> **Note:** The `Plan` CR itself stays in the main `target_namespace` even when `vm_target_namespace` sends migrated VMs somewhere else. Only the migrated VMs move.

One copy-offload wait is stricter than the rest:

> **Warning:** The copy-offload plan-secret wait is not best-effort. It raises `TimeoutError` after 60 seconds, listing the secrets it did find. The migration only starts waiting
> on `Plan` status once the secret appears.

## Migration

`execute_migration()` creates a separate `Migration` CR that points at the `Plan`. For cold migrations it passes no cutover time. For warm migrations it computes one from
the current UTC time and the configured offset.

From `utilities/migration_utils.py`:

```python
def get_cutover_value(current_cutover: bool = False) -> datetime:
    datetime_utc = datetime.now(pytz.utc)
    if current_cutover:
        return datetime_utc

    return datetime_utc + timedelta(minutes=int(py_config["mins_before_cutover"]))
```

The `Migration` CR is deliberately tiny:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: Migration
metadata:
  name: auto-abc123-source-vsphere-8-0-3-abc-cold
  namespace: auto-abc123
spec:
  plan:
    name: auto-abc123-source-vsphere-8-0-3-abc-cold
    namespace: auto-abc123
  cutover: "2026-01-02T03:04:05Z"   # warm migrations only, UTC, second precision
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
- Final `Succeeded` and `Failed` states come from advisory conditions on the `Plan`: conditions with `category: Advisory` and `status: True`, matched against
  `Plan.Status.SUCCEEDED` / `Plan.Status.FAILED`.
- Detailed per-VM failure steps such as `PreHook`, `PostHook`, or `DiskTransfer` come from the `Plan` CR, not the `Migration`: `status.migration.vms[].pipeline[]`, read from
  `plan.instance.status.migration.vms` because the controller updates the `Plan` first.
- Dual and simultaneous migrations have dedicated helpers: `wait_for_dual_migration_completion()` waits for both plans and raises `MigrationPlanExecError` as soon as either one
  reports `Failed`, while `wait_for_concurrent_migration_execution()` asserts that all listed plans are in `Executing` at the same time, within 120 seconds.

Two gating rules decide whether a warm migration can even be attempted. The first is cluster readiness:

> **Note:** Before the suite starts creating plans and migrations, it waits up to five minutes for all `forklift-*` pods in `mtv_namespace` to be `Running` or `Succeeded`, and it
> requires a controller pod to exist.

The second is provider support, enforced at collection time:

> **Note:** `pytest_collection_modifyitems` in `conftest.py` skips every `warm` test when the configured source provider is `openstack`, `openshift`, `ova`, or `hyperv`.

## Conversion

Deep Inspection tests create a `Conversion` CR in addition to the usual chain. A standalone conversion is built from the provider connection secret, the source VM inventory ID, and
optionally a snapshot MOREF:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: Conversion
metadata:
  name: auto-abc123-cold
  namespace: auto-abc123
spec:
  connection:
    secret:
      name: auto-abc123-di-connection
      namespace: auto-abc123
  type: DeepInspection
  vm:
    id: vm-101
    name: mtv-win2019-3disks
    type: VirtualMachine
  settings:
    SNAPSHOT_MOREF: datastore1:mtv-win2019-3disks/snap   # only when reusing an existing snapshot
  targetNamespace: auto-abc123
  vddkImage: /var/run/vddk
```

| Field | Source in the suite |
| --- | --- |
| `type` | Always `DeepInspection`; the wrapper has no constant for it, so the suite defines it locally |
| `connection.secret` | Connection secret built from the source provider secret plus the provider URL and SSL fingerprint |
| `vm.id`, `vm.name`, `vm.type` | Inventory VM data |
| `vddkImage` | Required for Deep Inspection. Omitting it is how the validation test triggers `VDDKImageNotSet` |
| `settings.SNAPSHOT_MOREF` | Set only when the test reuses an existing snapshot, which skips the snapshot-creation stages |
| `targetNamespace` | Namespace where the conversion pod runs |

The suite polls `status.phase` and `status.stage`, and treats `Succeeded`, `Failed`, and `Canceled` as terminal. Failures read the `Critical` conditions’ messages from the same
status object. Cancellation patches the status subresource directly to `phase: Canceled`, `stage: Finished`, mirroring the Forklift controller’s own cancel path.

> **Note:** Plan-driven Deep Inspection uses the same `Conversion` machinery but runs it as part of a warm `Plan`, with `runPreflightInspection: false` asserting that no DI
> results were produced.

## Related Resources and Cleanup

A few supporting resources show up often enough that they are worth calling out directly:

- The main `target_namespace` is labeled with restricted pod-security settings and `mutatevirtualmachines.kubemacpool.io=ignore`.
- Custom namespaces created through `get_or_create_namespace()` are created with the same standard labels and waited to `Active`.
- OpenShift source-provider tests create a separate `${session_uuid}-source-vms` namespace for source-side CNV VMs.
- Copy-offload tests create a storage credential `Secret` in the run namespace and may also rely on the plan-specific secret Forklift creates later.
- `aap` hook tests create an AWX token `Secret` in `mtv_namespace` named `${session_uuid}-awx-aap`.
- Every object created through `create_and_store_resource()` is registered in `fixture_store["teardown"]` under its kind.

Cleanup happens in two layers:

- `cleanup_migrated_vms` removes migrated VMs at class teardown, using `vm_target_namespace` when you set one.
- Session teardown first cancels every `Migration` and archives every `Plan`, then deletes what is left: `Provider`, `Secret`, `StorageMap`, `NetworkMap`,
  `NetworkAttachmentDefinition`, `Conversion`, `Host`, `Namespace`, plus the `Pod` and `VirtualMachine` objects the migration itself created. It also deletes OpenStack
  `VolumeSnapshot`s, waits for leftover session pods to disappear, and verifies that DataVolumes, PVCs, and PVs are gone from the run namespace.
- Source-side clones are removed through the provider APIs, not the cluster: vCenter or ESXi for VMware, OpenStack, RHV, and Hyper-V.

> **Warning:** `--skip-teardown` leaves those resources behind on purpose. Use it only when you want leftover `Plan`, `Migration`, `Provider`, PVC, VM, and namespace objects for
> debugging.
