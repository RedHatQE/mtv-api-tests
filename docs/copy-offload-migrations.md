# Copy-Offload Migrations

Copy-offload migrations in `mtv-api-tests` cover the VMware-to-OpenShift path where MTV can let the storage array move VM disk data instead of using the standard VDDK copy path. In
practice, that means a vSphere source provider, shared storage between vSphere and OpenShift, a `StorageMap` with `offloadPlugin.vsphereXcopyConfig`, and the right storage
credentials.

This project does more than validate a single happy path. The existing copy-offload coverage includes thin and thick disks, snapshots, RDM, multi-datastore layouts, warm migration,
non-XCOPY fallback, VM naming edge cases, scale, per-host populator throttling, dedicated migration hosts, and concurrent XCOPY/VDDK execution.

## What You Need

- A vSphere source provider. The copy-offload validation in this project fails fast for non-VMware sources.
- Shared storage between vSphere and OpenShift.
- A block-backed OpenShift storage class. Every copy-offload test builds its `StorageMap` destination
  with `volume_mode="Block"`, which emits `volumeMode: Block` alongside the storage class.
- MTV installed and healthy.
- A cloneable test VM or template with working guest access. Several scenarios power VMs on, wait for guest info, create snapshots, or validate guest connectivity after migration.
  A clone method: `vib` or `ssh`.

> **Warning:** The repository's copy-offload guidance assumes SAN or block-backed storage. The existing project docs explicitly call out NFS as unsupported for copy-offload
> scenarios.

For older MTV environments, the project docs include this feature-gate example:

```yaml
spec:
  feature_copy_offload: 'true'
```

> **Note:** `mtv-api-tests` does not toggle that feature gate for you. If your MTV version requires it, enable it before running copy-offload scenarios.

## Copy-Offload Provider Config

The copy-offload settings live under the VMware provider entry in `.providers.json`. This is the actual `copyoffload` block from `.providers.json.example`:

```jsonc
"copyoffload": {
  # Supported storage_vendor_product values:
  # - "ontap"           (NetApp ONTAP)
  # - "vantara"         (Hitachi Vantara)
  # - "primera3par"     (HPE Primera/3PAR)
  # - "pureFlashArray"  (Pure Storage FlashArray)
  # - "powerflex"       (Dell PowerFlex)
  # - "powermax"        (Dell PowerMax)
  # - "powerstore"      (Dell PowerStore)
  # - "infinibox"       (Infinidat InfiniBox)
  # - "flashsystem"     (IBM FlashSystem)
  "storage_vendor_product": "ontap",

  # Primary datastore for copy-offload operations (required)
  # This is the vSphere datastore ID (e.g., "datastore-12345") where VMs reside
  # Get via vSphere: Datacenter → Storage → Datastore → Summary → More Objects ID
  "datastore_id": "datastore-12345",

  # Optional: Secondary datastore for multi-datastore copy-offload tests
  # Only needed when testing VMs with disks spanning multiple datastores
  # When specified, tests can validate copy-offload with disks on different datastores
  "secondary_datastore_id": "datastore-67890",

  # Optional: Non-XCOPY datastore for mixed datastore tests
  # This should be a datastore that does NOT support XCOPY/VAAI primitives
  # Used for testing VMs with disks on both XCOPY and non-XCOPY datastores
  "non_xcopy_datastore_id": "datastore-99999",

  "default_vm_name": "rhel9-template",

  # Optional: vSphere resource pool for VM placement during cloning
  # Priority: this value -> target ESXi host's pool -> source VM's pool -> cluster search
  # "resource_pool": "<VSPHERE RESOURCE POOL NAME>",

  "storage_hostname": "storage.example.com",
  "storage_username": "admin",
  "storage_password": "your-password-here",  # pragma: allowlist secret

  # Vendor-specific fields (configure based on your storage_vendor_product):
  # IMPORTANT: Only configure the fields for your selected storage_vendor_product.
  # For example, if storage_vendor_product == "ontap", only configure ontap_svm.
  # You may leave other vendor-specific fields blank or remove them from your config.
  # Note: Both datastore_id and secondary_datastore_id (if used) must be on the
  # same storage array and support XCOPY/VAAI primitives for copy-offload to work.
  # See forklift vsphere-xcopy-volume-populator code/README for details

  # NetApp ONTAP (required for "ontap"):
  "ontap_svm": "vserver-name",

  # Hitachi Vantara (required for "vantara"):
  "vantara_storage_id": "123456789",  # Storage array serial number
  "vantara_storage_port": "443",  # Storage API port
  "vantara_hostgroup_id_list": "CL1-A,1:CL2-B,2:CL4-A,1:CL6-A,1",  # IO ports and host group IDs

  # Pure Storage FlashArray (required for "pureFlashArray"):
  # Get with: printf "px_%.8s" $(oc get storagecluster -A -o=jsonpath='{.items[?(@.spec.cloudStorage.provider=="pure")].status.clusterUid}')
  "pure_cluster_prefix": "px_a1b2c3d4",

  # Dell PowerFlex (required for "powerflex"):
  # Get from vxflexos-config ConfigMap in vxflexos or openshift-operators namespace
  "powerflex_system_id": "system-id",

  # Dell PowerMax (required for "powermax"):
  # Get from ConfigMap in powermax namespace used by CSI driver
  "powermax_symmetrix_id": "000123456789",

  # HPE Primera/3PAR, Dell PowerStore, Infinidat InfiniBox, IBM FlashSystem:
  # No additional vendor-specific fields required - use only the common fields above

  # Optional: extra Secret stringData keys not covered by vendor fields above.
  # Keys must match Forklift Secret names (uppercase). Example is for PowerMax only.
  # Overridden by the COPYOFFLOAD_STORAGE_SECRET_EXTRA env var (JSON object).
  # "storage_secret_extra": {
  #   "POWERMAX_PORT_GROUP_NAME": "mtv_group_pg",
  #   "STORAGE_SKIP_SSL_VERIFICATION": "true"
  # },

  # Optional: dedicated ESXi host(s) for XCOPY data extraction. Maps to StorageMap
  # offloadPlugin.vsphereXcopyConfig.dedicatedMigrationHosts. Random per-disk selection
  # when more than one host is listed.
  # "dedicated_migration_hosts": ["host-3078"],

  # ESXi SSH configuration (optional, for SSH-based cloning):
  # Can be overridden via environment variables: COPYOFFLOAD_ESXI_HOST, COPYOFFLOAD_ESXI_USER, COPYOFFLOAD_ESXI_PASSWORD
  "esxi_clone_method": "ssh",  # "vib" (default) or "ssh"
  "esxi_host": "your-esxi-host.example.com",  # required for ssh method
  "esxi_user": "root",  # required for ssh method
  "esxi_password": "your-esxi-password",  # pragma: allowlist secret # required for ssh method

  # RDM testing (optional, for RDM disk tests):
  # Note: datastore_id must be a VMFS datastore for RDM disk support
  "rdm_lun_uuid": "naa.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

The `copyoffload_config` session fixture validates five fields before any copy-offload test body runs.
A missing value fails the session with a message naming the field or the environment variable to set:

- `storage_vendor_product` and `datastore_id` must be present in `.providers.json`
- `storage_hostname`, `storage_username`, and `storage_password` must resolve from `.providers.json` or from their `COPYOFFLOAD_*` environment variables

Add these when you want advanced scenarios:

- `secondary_datastore_id` for multi-datastore tests
- `non_xcopy_datastore_id` for mixed and fallback tests
- `rdm_lun_uuid` for RDM tests
- `dedicated_migration_hosts` for the dedicated-host tests
- `default_vm_name` if your copy-offload-ready template differs from the default test data

Each advanced key has its own class-scoped guard fixture that fails fast with a specific message
rather than letting the test fail later: `multi_datastore_config`, `mixed_datastore_config`,
`rdm_config`, and `configured_dedicated_hosts`.

> **Note:** The `# pragma: allowlist secret` comments in `.providers.json.example` are there for repository tooling. They are not valid JSON and must be removed from your real
> `.providers.json`.

`default_vm_name` is especially useful when your environment has a single known-good copy-offload template. The suite applies that override to cloned VM scenarios so you do not
have to change every test entry by hand.

## Environment Variable Overrides

The repository lets you override any copy-offload credential from the environment, and environment values always win over `.providers.json`:

```python
env_var_name = f"COPYOFFLOAD_{credential_name.upper()}"
return os.getenv(env_var_name) or copyoffload_config.get(credential_name)
```

That pattern works for the common storage credentials:

- `COPYOFFLOAD_STORAGE_HOSTNAME`
- `COPYOFFLOAD_STORAGE_USERNAME`
- `COPYOFFLOAD_STORAGE_PASSWORD`

It also works for vendor-specific and ESXi-specific values such as:

- `COPYOFFLOAD_ONTAP_SVM`
- `COPYOFFLOAD_VANTARA_HOSTGROUP_ID_LIST`
- `COPYOFFLOAD_ESXI_HOST`
- `COPYOFFLOAD_ESXI_USER`
- `COPYOFFLOAD_ESXI_PASSWORD`

There is one more override for raw secret keys. `COPYOFFLOAD_STORAGE_SECRET_EXTRA` takes a JSON object
of `Secret` `stringData` keys and merges it into the copy-offload storage secret. Values from
`storage_secret_extra` in `.providers.json` are applied first, then the environment variable
overrides matching keys:

```bash
export COPYOFFLOAD_STORAGE_SECRET_EXTRA='{"POWERMAX_PORT_GROUP_NAME": "mtv_group_pg", "STORAGE_SKIP_SSL_VERIFICATION": "true"}'
```

Extra entries win over the vendor-mapped fields, so you can correct a vendor key without editing
`.providers.json`. JSON booleans are normalized to lowercase `true`/`false` before they reach
the `Secret`.

> **Tip:** A good pattern is to keep the structural values in `.providers.json` and inject the sensitive values through environment variables at runtime.

## Supported Storage Vendors

Use these exact `storage_vendor_product` values. They come directly from the repository's copy-offload constants and secret-mapping logic.

| `storage_vendor_product` | Storage platform | Extra required fields |
| --- | --- | --- |
| `ontap` | NetApp ONTAP | `ontap_svm` |
| `vantara` | Hitachi Vantara | `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list` |
| `pureFlashArray` | Pure Storage FlashArray | `pure_cluster_prefix` |
| `powerflex` | Dell PowerFlex | `powerflex_system_id` |
| `powermax` | Dell PowerMax | `powermax_symmetrix_id` |
| `powerstore` | Dell PowerStore | None beyond base storage credentials |
| `primera3par` | HPE Primera / 3PAR | None beyond base storage credentials |
| `infinibox` | Infinidat InfiniBox | None beyond base storage credentials |
| `flashsystem` | IBM FlashSystem | None beyond base storage credentials |

## Storage Secrets

In `mtv-api-tests`, you usually do not create the copy-offload storage secret by hand. The suite creates it automatically from the VMware provider's `copyoffload` block.

That matters because:

- The secret values can come from `.providers.json` or environment variables.
- The secret is created in the same target namespace where the suite creates the `StorageMap` and `Plan`.
- The `offloadPlugin` can reference the secret by name, without extra manual wiring.

The suite always creates these base secret keys:

- `STORAGE_HOSTNAME`
- `STORAGE_USERNAME`
- `STORAGE_PASSWORD`

It then adds vendor-specific keys such as `ONTAP_SVM`, `STORAGE_ID`, `STORAGE_PORT`,
`HOSTGROUP_ID_LIST`, `PURE_CLUSTER_PREFIX`, `POWERFLEX_SYSTEM_ID`, or `POWERMAX_SYMMETRIX_ID`,
depending on `storage_vendor_product`. Those keys are optional only when the vendor entry has an empty
field list, which is the case for `primera3par`, `powerstore`, `infinibox`, and `flashsystem`.

Any key from `storage_secret_extra` or `COPYOFFLOAD_STORAGE_SECRET_EXTRA` is merged in last and
overrides the vendor-mapped value for the same key.

After the `Migration` CR is created, the suite waits up to 60 seconds for Forklift to create the
plan-specific populator secret, which it matches by the `plan-name` and `isPopulator` labels or by a
`<plan-name>-*` name prefix. If the secret never appears, the run stops with a `TimeoutError` that
lists the secrets actually present in the namespace.

> **Note:** Forklift creates that secret when the migration starts, not when the `Plan` reaches Ready.
> The suite therefore waits for it during `execute_copyoffload_migration()`, never inside
> `create_plan_resource()`.
>
> **Note:** This automatic secret handling is specific to how `mtv-api-tests` drives copy-offload. It
> removes a lot of manual setup from the test workflow.

## StorageMap and Plan Behavior

The core of copy-offload in this project is the `offloadPlugin` block. The tests build it like this:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
    }
}
```

The storage map entries then attach that plugin to the source datastore mapping and set the destination
for block-backed PVCs. `access_mode` is optional and no copy-offload test passes it, so only
`storageClass` and `volumeMode` are emitted:

```python
storage_map_list.append({
    "destination": destination_config,
    "source": {"id": ds_id},
    "offloadPlugin": offload_plugin_config,
})
```

The project also changes plan behavior for copy-offload runs. The volume populator framework needs a predictable PVC name, so the plan helper overrides any template you configured:

```python
if copyoffload:
    plan_kwargs["pvc_name_template"] = "pvc"

plan = create_and_store_resource(**plan_kwargs)
```

The plan secret is not waited for here. It is waited for right after the `Migration` CR is created, inside `execute_copyoffload_migration()`:

```python
create_and_store_resource(
    client=ocp_admin_client,
    fixture_store=fixture_store,
    resource=Migration,
    namespace=target_namespace,
    plan_name=plan.name,
    plan_namespace=plan.namespace,
    cut_over=cut_over,
)

wait_for_copyoffload_plan_secret(
    ocp_admin_client=ocp_admin_client,
    plan=plan,
    namespace=target_namespace,
)
```

And when the suite creates the VMware `Provider` for a provider that has a `copyoffload` section, it adds this annotation:

```python
provider_annotations["forklift.konveyor.io/empty-vddk-init-image"] = "yes"
```

That is the repository's way of steering the provider toward the copy-offload path instead of a VDDK-only setup.

The difference is explicit in the concurrent XCOPY/VDDK scenario: the XCOPY `StorageMap` must contain `offloadPlugin`, and the VDDK `StorageMap` must not.

> **Tip:** If you want a fast sanity check that your environment is wired correctly, start with `test_copyoffload_thin_migration`. It uses the same `offloadPlugin` structure as the
> advanced scenarios, but with fewer moving parts.

## Clone Methods

The suite supports both copy-offload clone methods exposed by the populator: `vib` and `ssh`.

### VIB

`vib` is the default. If you omit `esxi_clone_method`, the repository leaves the provider's clone method alone and relies on the default VIB behavior.

Use `vib` when:

- Your ESXi environment allows community-level VIB installation.
- You do not want the suite to manage ESXi SSH credentials.

> **Note:** The repository does not perform extra VIB-specific setup. It assumes the populator and ESXi host permissions are already ready for the VIB path.

### SSH

If you set `esxi_clone_method` to `ssh`, the suite patches the VMware `Provider` so MTV uses SSH-based cloning:

```python
patch = {"spec": {"settings": {"esxiCloneMethod": clone_method}}}
ResourceEditor(patches={self.ocp_resource: patch}).update()
```

It then retrieves the provider-generated public key from the `offload-ssh-keys-<provider>-public` secret and installs a restricted key on the ESXi host. The restricted command is
taken directly from the ESXi helper:

```python
command_template = (
    'command="python /vmfs/volumes/{datastore_name}/secure-vmkfstools-wrapper.py",'
    "no-port-forwarding,no-agent-forwarding,no-X11-forwarding {public_key}"
)
```

For SSH mode, you must provide:

- `esxi_host`
- `esxi_user`
- `esxi_password`

> **Warning:** SSH mode temporarily updates `/etc/ssh/keys-root/authorized_keys` on the ESXi host.
> The suite removes the key during teardown, but it is still a real host-side change.
>
> **Tip:** SSH mode is a good choice when you want a fully test-managed setup path. The suite handles
> the provider patch, key installation, and cleanup for you.

## Fallback Modes

Copy-offload is not all-or-nothing in this repository. The existing tests explicitly cover cases where some or all disks live on a datastore that does not support XCOPY/VAAI.

There are two main fallback patterns:

- Mixed-datastore fallback: one disk uses an XCOPY-capable datastore and another disk lives on `non_xcopy_datastore_id`.
- Full non-XCOPY fallback: the VM is relocated to `non_xcopy_datastore_id`, and added disks are placed there too.

The storage-map helper keeps the `offloadPlugin` on the non-XCOPY mapping so Forklift can exercise fallback behavior:

```python
storage_map_list.append({
    "destination": destination_config,
    "source": {"id": non_xcopy_datastore_id},
    "offloadPlugin": offload_plugin_config,
})
```

The full fallback case is modeled directly in the repository like this:

```python
"test_copyoffload_fallback_large_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "guest_agent": True,
            "clone": True,
            "target_datastore_id": "non_xcopy_datastore_id",
            "disk_type": "thin",
            "add_disks": [
                {
                    "size_gb": 100,
                    "provision_type": "thin",
                    "datastore_id": "non_xcopy_datastore_id",
                },
            ],
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
}
```

The two fallback classes assert the transfer mechanism in opposite ways, which is the part worth knowing before you read a failure:

- `TestCopyoffloadFallbackLargeMigration` calls `verify_xcopy_used()` with `expected_xcopy_used=False`, so every disk is expected to report `xcopyUsed=0`.
  `TestCopyoffloadMixedDatastoreMigration` calls `verify_xcopy_used_per_datastore()` and asserts per
  datastore: `xcopyUsed=1` on `datastore_id`, `xcopyUsed=0` on `non_xcopy_datastore_id`. It
  cross-checks populate pod logs against the `xcopyUsed` annotation on each `DiskTransfer` task of
  the `Plan` CR.

> **Warning:** `non_xcopy_datastore_id` must point to a real datastore that does not support XCOPY/VAAI. If it is missing, the `mixed_datastore_config` fixture fails the class
> before migration starts.

## Dedicated Migration Hosts

`dedicated_migration_hosts` in your provider `copyoffload` block is a list of vSphere managed object IDs, for example `["host-3078"]`. It is passed straight through to the
`StorageMap`:

```python
offload_plugin_config = {
    "vsphereXcopyConfig": {
        "secretRef": copyoffload_storage_secret.name,
        "storageVendorProduct": storage_vendor_product,
        "dedicatedMigrationHosts": configured_dedicated_hosts,
    }
}
```

`TestCopyoffloadDedicatedMigrationHost` then verifies that XCOPY data extraction ran on one of those
hosts instead of each VM's registered ESXi host. It reads the `sourceHost` label from the cached
populate pod logs and compares it against the configured set.

Forklift picks a host at random per disk, so the test adapts its assertions:

- With exactly one configured host, every disk shares one throttle budget. The test asserts peak concurrency reaches `min(limit, disk_count)` and that `PopulatorThrottled` events
  appear.
- With more than one configured host, only the per-host ceiling is enforced, and observing fewer distinct hosts than configured logs a warning instead of failing.

To make that check meaningful, the plan sets `pin_to_non_dedicated_host`, which resolves a single ESXi
host outside the dedicated list and pins every VM clone to it before migration. Without the pin, a VM
placed by DRS on a dedicated host would satisfy the membership check even if the dedicated-host
feature did nothing.

> **Warning:** The lab needs at least one ESXi host that is not in `dedicated_migration_hosts`.
> `resolve_non_dedicated_esxi_host()` raises a `ValueError` when every inventoried host is configured
> as dedicated.

The negative case lives in `TestCopyoffloadDedicatedMigrationHostInvalidId`. It resolves an ESXi host ID
that is absent from the vSphere inventory and puts that ID in `dedicatedMigrationHosts`. Forklift
validates the host only inside the populate pod, so the `Plan` and `Migration` CRs are created
successfully and only the populate pod fails. The test therefore expects `test_migrate_vms` to raise
`MigrationPlanExecError` and then asserts the populate pod failure reason contains
`didn't find any host with id <id>`.

## Populator And VM Throttling

Two classes validate the `ForkliftController` in-flight limits by lowering them for the duration of the class and restoring the previous values afterwards.

`TestCopyoffloadPopulatorThrottlingMigration` uses the `populator_inflight_forkliftcontroller` fixture,
which sets `spec.controller_max_populator_inflight` to `POPULATOR_INFLIGHT_LIMIT` (2) and waits for
the populator controller deployment to pick up the matching `MAX_POPULATOR_INFLIGHT` env value. The VM
has four added disks, and the test asserts:

- populate pods carry the `sourceHost` label, and all pods agree on one host
- at least `disk_count - limit` PVCs recorded a `PopulatorThrottled` event
- observed peak concurrency per host stays at or below the limit and reaches `min(limit, disk_count)`

`TestCopyoffloadVmPopulatorThrottlingMigration` uses `vm_populator_inflight_forkliftcontroller` and
lowers two limits at once: `controller_max_vm_inflight` to `VM_INFLIGHT_LIMIT` (1) and
`controller_max_populator_inflight` to `VM_POPULATOR_INFLIGHT_LIMIT` (2). It migrates two three-disk
VMs and splits verification into two steps: `test_verify_vm_inflight_throttling` for peak concurrent
VMs per host, then `test_verify_populator_throttling` for labels, events, and peak populate pods.
Because VMs migrate sequentially, the expected throttled count is computed per VM batch
(`vm_count * max(0, disks_per_vm - limit)`) rather than the default `pod_count - limit`.

> **Warning:** Both fixtures mutate cluster-wide MTV settings and take a file lock so parallel
> pytest-xdist workers cannot patch the same `ForkliftController`. Do not run the populator-throttling
> and VM-throttling classes against the same cluster at the same time. The file lock also serializes a
> run against itself: expect a `TimeoutError` if another worker holds it.

Both throttling fixtures also disable DRS for the cloned VMs (`disable_drs_for_vms`) and, for the
VM-throttling case, pin every clone to the first VM's host (`clone_to_same_host`). Without both, DRS
could move a VM to a different ESXi host mid-run and the per-host counters would stop meaning
anything.

## Advanced Copy-Offload Scenarios

The copy-offload test matrix in this repository is broader than the basic thin-disk path.
Every row below is a real test class in `tests/copyoffload/test_copyoffload_migration.py`,
paired with the plan-config key it reads.

| Test class | Plan-config key | What it validates |
| --- | --- | --- |
| `TestCopyoffloadThinMigration` | `test_copyoffload_thin_migration` | Baseline thin-disk XCOPY acceleration |
| `TestCopyoffloadThickLazyMigration` | `test_copyoffload_thick_lazy_migration` | Thick lazy zeroed disk |
| `TestCopyoffloadThickEagerMigration` | `test_copyoffload_thick_eager_migration` | Thick eager zeroed disk |
| `TestCopyoffloadMultiDiskMigration` | `test_copyoffload_multi_disk_migration` | One added disk on the primary datastore |
| `TestCopyoffloadDualDiskMixedThinThickMigration` | `test_copyoffload_dual_disk_mixed_thin_thick_migration` | Thin boot disk plus a thick-lazy added disk |
| `TestCopyoffloadMultiDiskDifferentPathMigration` | `test_copyoffload_multi_disk_different_path_migration` | Added disk under a custom folder such as `shared_disks` |
| `TestCopyoffloadMultiDatastoreMigration` | `test_copyoffload_multi_datastore_migration` | Disks split across primary and secondary XCOPY datastores |
| `TestCopyoffloadMultiDiskDifferentDatastorePathMigration` | `test_copyoffload_multi_disk_different_datastore_path_migration` | Added disk on secondary datastore, custom folder |
| `TestCopyoffloadMixedDatastoreMigration` | `test_copyoffload_mixed_datastore_migration` | Accelerate on one datastore, fall back on another |
| `TestCopyoffloadFallbackLargeMigration` | `test_copyoffload_fallback_large_migration` | Large VM and added disk entirely on a non-XCOPY datastore |
| `TestCopyoffloadRdmVirtualDiskMigration` | `test_copyoffload_rdm_virtual_disk_migration` | `virtualMode` RDM LUN migrated as a SCSI LUN device |
| `TestCopyoffloadRdmPhysicalDiskMigration` | `test_copyoffload_rdm_physical_disk_migration` | `physicalMode` independent-persistent RDM LUN |
| `TestCopyoffloadWarmRdmVirtualDiskMigration` | `test_copyoffload_warm_rdm_virtual_disk_migration` | Warm migration of a VM with an RDM virtual disk |
| `TestCopyoffloadThinSnapshotsMigration` | `test_copyoffload_thin_snapshots_migration` | Two source snapshots plus a data-integrity marker on a thin disk |
| `TestCopyoffloadThickLazySnapshotsMigration` | `test_copyoffload_thick_lazy_snapshots_migration` | Same snapshot flow on a thick lazy disk |
| `TestCopyoffloadThickEagerSnapshotsMigration` | `test_copyoffload_thick_eager_snapshots_migration` | Same snapshot flow on a thick eager disk |
| `TestCopyoffload2TbVmSnapshotsMigration` | `test_copyoffload_2tb_vm_snapshots_migration` | 2 TB added disk with snapshots and a data-integrity marker |
| `TestCopyoffloadIndependentPersistentDiskMigration` | `test_copyoffload_independent_persistent_disk_migration` | Independent persistent added disk |
| `TestCopyoffloadIndependentNonpersistentDiskMigration` | `test_copyoffload_independent_nonpersistent_disk_migration` | Independent nonpersistent added disk |
| `TestCopyoffloadLargeVmMigration` | `test_copyoffload_large_vm_migration` | 1 TB added disk |
| `TestCopyoffload10MixedDisksMigration` | `test_copyoffload_10_mixed_disks_migration` | Ten added disks alternating thin and thick-lazy |
| `TestCopyoffloadPopulatorThrottlingMigration` | `test_copyoffload_populator_throttling_migration` | Per-host populator limit and `PopulatorThrottled` events |
| `TestCopyoffloadVmPopulatorThrottlingMigration` | `test_copyoffload_vm_populator_throttling_migration` | Combined VM and populator limits across two VMs |
| `TestCopyoffloadDedicatedMigrationHost` | `test_copyoffload_dedicated_migration_host_migration` | XCOPY extraction routed to `dedicatedMigrationHosts` |
| `TestCopyoffloadDedicatedMigrationHostInvalidId` | `test_copyoffload_dedicated_migration_host_invalid_id_migration` | Unknown host ID fails the populate pod, not the Plan |
| `TestCopyoffloadNonconformingNameMigration` | `test_copyoffload_nonconforming_name_migration` | Uppercase and underscore source name sanitized for Kubernetes |
| `TestCopyoffloadWarmMigration` | `test_copyoffload_warm_migration` | Live warm migration with a cutover value |
| `TestCopyoffloadScaleMigration` | `test_copyoffload_scale_migration` | Five thick-lazy VMs in one plan |
| `TestSimultaneousCopyoffloadMigrations` | `test_simultaneous_copyoffload_migrations` | Two copy-offload plans executing simultaneously |
| `TestConcurrentXcopyVddkMigration` | `test_concurrent_xcopy_vddk_migration` | One copy-offload plan and one VDDK plan running together |

`TestConcurrentXcopyVddkMigration` carries `@pytest.mark.copyoffload` and exercises a mixed XCOPY-and-VDDK scenario, so its name describes the pair rather than the
feature. Marker selection and the collection-time vSphere gate both key off `@pytest.mark.copyoffload`, so `-m copyoffload` still collects it.

### Snapshot Scenarios

The four snapshot classes share `CopyoffloadSnapshotBase`, which adds one step before the standard sequence and one after it:

1. `test_create_snapshots_and_data_marker` powers the source VM on, waits for VMware guest info,
   creates `snapshots` snapshots named with the session UUID, and writes a data-integrity marker file
   over VMware Guest Operations.
2. The standard `test_create_storagemap` through `test_check_vms` steps run.
3. `test_check_data_integrity` starts the migrated VM over SSH, reads the marker file back, compares it to what was written pre-migration, and stops the VM again.

Snapshot VMs expect fallback, not acceleration: the shared base class calls `verify_xcopy_used()` with
`expected_xcopy_used=False`. These classes also carry the `copyoffload_snapshots` marker so you can run
them on their own.

### RDM Handling

RDM tests add their LUN through `add_disks` with an `rdm_type` key rather than `size_gb`:

```python
"test_copyoffload_rdm_virtual_disk_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "guest_agent": True,
            "clone": True,
            "add_disks": [
                {"rdm_type": "virtual"},
            ],
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
    "rdm_as_lun": True,
}
```

- `rdm_type: "virtual"` maps to `virtualMode`, `rdm_type: "physical"` to `physicalMode` plus `independent_persistent` disk mode.
- The LUN itself comes from `copyoffload.rdm_lun_uuid`, resolved when the disk is attached to the clone.
- The plan-level `rdm_as_lun: True` key sets `spec.rdmAsLun` on the `Plan` CR, which makes KubeVirt map the RDM as a LUN device with a SCSI bus instead of a regular virtio disk.
  `verify_rdm_disk_bus_types()` asserts at least one LUN-type disk exists on the migrated VM and that each one carries the SCSI bus.

> **Note:** The `Plan` CR only gets `spec.rdmAsLun` when the key is present, so `rdm_as_lun` is set on
> the cold RDM configs but deliberately omitted from the warm RDM config.
>
> **Note:** The project's copy-offload guide calls out RDM support only for Pure Storage, and it
> requires `datastore_id` to be a VMFS datastore for RDM scenarios. RDM disks are not supported on vSAN
> or NFS datastores.

The repository's multi-datastore scenario uses a symbolic secondary datastore key instead of hardcoding the MoID into every disk entry:

```python
"test_copyoffload_multi_datastore_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "guest_agent": True,
            "clone": True,
            "disk_type": "thin",
            "add_disks": [
                {
                    "size_gb": 30,
                    "disk_mode": "persistent",
                    "provision_type": "thin",
                    "datastore_id": "secondary_datastore_id",
                },
            ],
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
}
```

The non-conforming-name scenario is also deliberate. Its source config preserves uppercase letters and underscores in the cloned VMware name, and the suite then verifies that MTV
sanitizes the destination VM name to a Kubernetes-safe value.

> **Note:** In copy-offload plan configs, `name: "xcopy-template-test"` is a placeholder. When
> `default_vm_name` is set in your provider `copyoffload` block, the plan preparation step replaces it
> with that template name for every VM marked `clone: True`.

## What the Suite Automates for You

When you run copy-offload scenarios through `mtv-api-tests`, the project handles several setup steps automatically:

- It validates that the source provider is vSphere and that the `copyoffload` section exists.
- It creates the copy-offload storage secret from `.providers.json`, environment variables, and any `storage_secret_extra` entries.
- It creates `StorageMap` entries with `offloadPlugin.vsphereXcopyConfig`, including `dedicatedMigrationHosts` when the dedicated-host test asks for it.
- It annotates the VMware provider with `forklift.konveyor.io/empty-vddk-init-image: "yes"`.
- It patches `esxiCloneMethod` to `ssh` when you choose SSH cloning.
- It waits for Forklift to create the plan-specific copy-offload secret once the migration starts.
- It forces `pvc_name_template` to `pvc` for copy-offload plans.
- It captures populate pod logs during the migration so `test_check_xcopy_used` can verify `xcopyUsed` after MTV has already deleted the pods.

That is why the user-facing setup is mostly about getting the provider config, datastore IDs, storage array credentials, and clone method right.

## Running The Copy-Offload Markers

Three markers select copy-offload tests, and they are registered in `pytest.ini`:

| Marker | Selects |
| --- | --- |
| `copyoffload` | Every copy-offload test class |
| `copyoffload_sanity` | A curated core subset for quick validation |
| `copyoffload_snapshots` | The four snapshot classes built on `CopyoffloadSnapshotBase` |

The `copyoffload_sanity` subset covers `TestCopyoffloadRdmVirtualDiskMigration`,
`TestCopyoffloadMultiDatastoreMigration`, `TestCopyoffloadMixedDatastoreMigration`,
`TestCopyoffloadScaleMigration`, `TestCopyoffload10MixedDisksMigration`,
`TestCopyoffloadWarmMigration`, and `TestCopyoffloadThickEagerSnapshotsMigration`.

The full-suite command below is taken directly from the existing Job example:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

Replace the provider key with the exact key from your `.providers.json`, and replace the storage class with the block-backed class that maps to the same storage array as your
vSphere datastores. Add `-k <test_name>` after `-m copyoffload` to select a single test, for example `-m copyoffload -k test_copyoffload_thin_migration`.

> **Tip:** Start with `test_copyoffload_thin_migration`, then move to `test_copyoffload_multi_datastore_migration`, `test_copyoffload_mixed_datastore_migration`, or
> `test_concurrent_xcopy_vddk_migration` once the base path is working.
