# Copy-Offload Migrations

Copy-offload migrations in `mtv-api-tests` cover the VMware-to-OpenShift path where MTV can let the storage array move VM disk data instead of using the standard VDDK copy path. In practice, that means a vSphere source provider, shared storage between vSphere and OpenShift, a `StorageMap` with `offloadPlugin.vsphereXcopyConfig`, and the right storage credentials.

This project does more than validate a single happy path. The existing copy-offload coverage includes thin and thick disks, snapshots, RDM, multi-datastore layouts, warm migration, non-XCOPY fallback, VM naming edge cases, scale, and concurrent XCOPY/VDDK execution.

## What You Need

- A vSphere source provider. The copy-offload validation in this project fails fast for non-VMware sources.
- Shared storage between vSphere and OpenShift.
- A block-backed OpenShift storage class. The copy-offload tests map the destination as `ReadWriteOnce` with `Block` volume mode.
- MTV installed and healthy.
- A cloneable test VM or template with working guest access. Several scenarios power VMs on, wait for guest info, create snapshots, or validate guest connectivity after migration.
- A clone method: `vib` or `ssh`.

> **Warning:** The repository's copy-offload guidance assumes SAN or block-backed storage. The existing project docs explicitly call out NFS as unsupported for copy-offload scenarios.

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

The minimum fields that this project validates before running are:

- `storage_vendor_product`
- `datastore_id`
- `storage_hostname`
- `storage_username`
- `storage_password`

Add these when you want advanced scenarios:

- `secondary_datastore_id` for multi-datastore tests
- `non_xcopy_datastore_id` for mixed and fallback tests
- `rdm_lun_uuid` for RDM tests
- `default_vm_name` if your copy-offload-ready template differs from the default test data

> **Note:** The `# pragma: allowlist secret` comments in `.providers.json.example` are there for repository tooling. They are not valid JSON and must be removed from your real `.providers.json`.

`default_vm_name` is especially useful when your environment has a single known-good copy-offload template. The suite applies that override to cloned VM scenarios so you do not have to change every test entry by hand.

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

It then adds vendor-specific keys such as `ONTAP_SVM`, `STORAGE_ID`, `HOSTGROUP_ID_LIST`, `PURE_CLUSTER_PREFIX`, `POWERFLEX_SYSTEM_ID`, or `POWERMAX_SYMMETRIX_ID`, depending on `storage_vendor_product`.

After the plan is created, the suite also waits for Forklift to create the plan-specific secret used during copy-offload. If that secret never appears, the run continues long enough to fail with a clearer migration error.

> **Note:** This automatic secret handling is specific to how `mtv-api-tests` drives copy-offload. It removes a lot of manual setup from the test workflow.

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

The storage map entries then attach that plugin to the source datastore mapping and set the destination for block-backed PVCs:

```python
storage_map_list.append({
    "destination": {
        "storageClass": target_storage_class,
        "accessMode": "ReadWriteOnce",
        "volumeMode": "Block",
    },
    "source": {"id": ds_id},
    "offloadPlugin": offload_plugin_config,
})
```

The project also changes plan behavior for copy-offload runs:

```python
if copyoffload:
    plan_kwargs["pvc_name_template"] = "pvc"

plan = create_and_store_resource(**plan_kwargs)

if copyoffload:
    wait_for_plan_secret(ocp_admin_client, target_namespace, plan.name)
```

And when the suite creates the VMware `Provider` for copy-offload, it adds this annotation:

```python
provider_annotations["forklift.konveyor.io/empty-vddk-init-image"] = "yes"
```

That is the repository's way of steering the provider toward the copy-offload path instead of a VDDK-only setup.

The difference is explicit in the concurrent XCOPY/VDDK scenario: the XCOPY `StorageMap` must contain `offloadPlugin`, and the VDDK `StorageMap` must not.

> **Tip:** If you want a fast sanity check that your environment is wired correctly, start with `test_copyoffload_thin_migration`. It uses the same `offloadPlugin` structure as the advanced scenarios, but with fewer moving parts.

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

It then retrieves the provider-generated public key from the `offload-ssh-keys-<provider>-public` secret and installs a restricted key on the ESXi host. The restricted command is taken directly from the ESXi helper:

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

> **Warning:** SSH mode temporarily updates `/etc/ssh/keys-root/authorized_keys` on the ESXi host. The suite removes the key during teardown, but it is still a real host-side change.

> **Tip:** SSH mode is a good choice when you want a fully test-managed setup path. The suite handles the provider patch, key installation, and cleanup for you.

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
            "source_vm_power": "off",
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

> **Warning:** `non_xcopy_datastore_id` must point to a real datastore that does not support XCOPY/VAAI. If it is missing, the mixed and fallback scenarios fail fast before migration starts.

## Advanced Copy-Offload Scenarios

The copy-offload test matrix in this repository is broader than the basic thin-disk path.

| Scenario | What it validates | Key test name(s) |
| --- | --- | --- |
| Basic provisioning | Thin and thick-lazy disk copy-offload | `test_copyoffload_thin_migration`, `test_copyoffload_thick_lazy_migration` |
| Multi-disk layouts | Additional disks on the same datastore | `test_copyoffload_multi_disk_migration` |
| Custom datastore paths | Extra disks placed under a custom folder like `shared_disks` | `test_copyoffload_multi_disk_different_path_migration` |
| Multi-datastore | A VM with disks spanning primary and secondary XCOPY datastores | `test_copyoffload_multi_datastore_migration` |
| Mixed XCOPY / non-XCOPY | Some disks accelerate while others fall back | `test_copyoffload_mixed_datastore_migration` |
| Full fallback on non-XCOPY | Large VM and added disk entirely on a non-XCOPY datastore | `test_copyoffload_fallback_large_migration` |
| RDM | RDM virtual-disk migration using `rdm_lun_uuid` | `test_copyoffload_rdm_virtual_disk_migration` |
| Snapshots | Source snapshots before migration, including a 2 TB case | `test_copyoffload_thin_snapshots_migration`, `test_copyoffload_2tb_vm_snapshots_migration` |
| Disk modes | Independent persistent and independent nonpersistent disks | `test_copyoffload_independent_persistent_disk_migration`, `test_copyoffload_independent_nonpersistent_disk_migration` |
| Large and dense VMs | 1 TB VM and 10 mixed thin/thick disks | `test_copyoffload_large_vm_migration`, `test_copyoffload_10_mixed_disks_migration` |
| Warm copy-offload | Warm migration with cutover | `test_copyoffload_warm_migration` |
| Name edge cases | Non-Kubernetes VM names with uppercase letters and underscores | `test_copyoffload_nonconforming_name_migration` |
| Scale | Five copy-offload VMs in one run | `test_copyoffload_scale_migration` |
| Concurrency | Two copy-offload plans at once | `test_simultaneous_copyoffload_migrations` |
| XCOPY vs VDDK | One copy-offload plan and one standard VDDK plan running together | `test_concurrent_xcopy_vddk_migration` |

The repository's multi-datastore scenario uses a symbolic secondary datastore key instead of hardcoding the MoID into every disk entry:

```python
"test_copyoffload_multi_datastore_migration": {
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

The non-conforming-name scenario is also deliberate. Its source config preserves uppercase letters and underscores in the cloned VMware name, and the suite then verifies that MTV sanitizes the destination VM name to a Kubernetes-safe value.

> **Note:** The project docs currently call out RDM copy-offload support only for Pure Storage, and they require `datastore_id` to be a VMFS datastore for RDM scenarios.

## What the Suite Automates for You

When you run copy-offload scenarios through `mtv-api-tests`, the project handles several setup steps automatically:

- It validates that the source provider is vSphere and that the `copyoffload` section exists.
- It creates the copy-offload storage secret from `.providers.json` or environment variables.
- It creates `StorageMap` entries with `offloadPlugin.vsphereXcopyConfig`.
- It annotates the VMware provider with `forklift.konveyor.io/empty-vddk-init-image: "yes"`.
- It patches `esxiCloneMethod` to `ssh` when you choose SSH cloning.
- It waits for Forklift to create the plan-specific copy-offload secret.
- It forces `pvc_name_template` to `pvc` for copy-offload plans.

That is why the user-facing setup is mostly about getting the provider config, datastore IDs, storage array credentials, and clone method right.

## Running the Copy-Offload Marker

The repository's copy-offload guide uses the `copyoffload` pytest marker. This command is taken directly from the existing Job example:

```bash
uv run pytest -m copyoffload \
  -v \
  ${CLUSTER_HOST:+--tc=cluster_host:${CLUSTER_HOST}} \
  ${CLUSTER_USERNAME:+--tc=cluster_username:${CLUSTER_USERNAME}} \
  ${CLUSTER_PASSWORD:+--tc=cluster_password:${CLUSTER_PASSWORD}} \
  --tc=source_provider:vsphere-8.0.3.00400 \
  --tc=storage_class:my-block-storageclass
```

Replace the provider key with the exact key from your `.providers.json`, and replace the storage class with the block-backed class that maps to the same storage array as your vSphere datastores.

> **Tip:** Start with `test_copyoffload_thin_migration`, then move to `test_copyoffload_multi_datastore_migration`, `test_copyoffload_mixed_datastore_migration`, or `test_concurrent_xcopy_vddk_migration` once the base path is working.
