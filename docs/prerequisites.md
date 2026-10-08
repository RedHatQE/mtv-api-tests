# Prerequisites

`mtv-api-tests` runs live end-to-end migrations. It talks to a real OpenShift cluster, a real MTV installation, and a real source provider.
Before you run it, make sure the target cluster, source environment, storage, network, and RBAC are ready for destructive integration testing.

> **Warning:** Run this suite only in a lab or other disposable environment.
> The tests create and delete `Namespace`, `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Migration`, `Hook`, `Secret`,
> `NetworkAttachmentDefinition`, `Pod`, and `VirtualMachine` resources.
> Some scenarios also clone source VMs, change source power state, add disks, and create snapshots.

Example test definitions in `tests/tests_config/config.py` show that clearly:

```python
"test_copyoffload_thin_snapshots_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "guest_agent": True,
            "clone": True,
            "disk_type": "thin",
            "snapshots": 2,  # Number of snapshots to create on the source VM before migration
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
},
```

Choose source VMs that match the scenario you plan to run. Do not point the suite at production VMs or production-only datastores.

## Local tooling

- Python 3.12 or 3.13, and [`uv`](https://docs.astral.sh/uv/) for environment and test execution.
- `oc` for cluster inspection, and for the `oc adm must-gather` call the data collector makes on failures.
- Podman or Docker if you run the suite from the container image.
- `virtctl` is not required. The suite downloads it from the cluster when it is missing from `PATH`.

> **Note:** Jenkins and rootcoz are optional. Jenkins orchestration is used by the project team, but nothing in the
> suite requires it. rootcoz is only used by the optional `--analyze-with-ai` flag; without it the suite logs a
> warning and disables AI analysis.

## Minimum environment

Every run needs all of the following:

- A reachable OpenShift API endpoint.
- Valid OpenShift credentials for the test runner.
- MTV installed and healthy in `openshift-mtv`, unless you intentionally override `mtv_namespace`.
- OpenShift Virtualization installed on the destination cluster.
- A usable destination `storage_class`.
- A `source_provider` value that matches a key in `.providers.json`.

The cluster client resolves credentials from `pytest-testconfig` first and falls back to environment variables:

```python
def get_cluster_client() -> DynamicClient:
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
```

> **Note:** `CLUSTER_VERIFY_SSL` has inverted semantics relative to `insecure_verify_skip`: `CLUSTER_VERIFY_SSL=true`
> means `insecure_verify_skip=False`. It wins over the `--tc` value when both are set.

The built-in test config also defines the key global defaults:

```python
insecure_verify_skip: str = "true"
source_provider_insecure_skip_verify: str = "false"
mtv_namespace: str = "openshift-mtv"
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
mins_before_cutover: int = 5
plan_wait_timeout: int = 3600
```

> **Note:** `insecure_verify_skip` controls TLS verification for the OpenShift API connection.
> `source_provider_insecure_skip_verify` is separate and defaults to `false`, so source-provider certificate verification is on unless you change it.

Before tests start, the suite checks `forklift-*` pods in the MTV namespace and fails if the `forklift-controller` pod is missing or any `forklift` pod is not healthy.

## Source provider configuration

The code supports these source provider types, taken from the `oneOf` branches in `providers_schema.json`:

| Source provider | Cold migration | Warm migration | Copy-offload |
| --- | --- | --- | --- |
| `vsphere` | Yes | Yes | Yes |
| `ovirt` / RHV | Yes | Yes | No |
| `openstack` | Yes | No | No |
| `openshift` | Yes | No | No |
| `ova` | Yes | No | No |
| `hyperv` | Yes | No | No |

vSphere also supports direct ESXi connections. Set `"endpoint_type": "esxi"` on the provider entry and point
`clone_provider` at a separate vCenter entry, because cloning still goes through vCenter.

Your `.providers.json` file must exist, must be non-empty, and must contain a key that exactly matches the `source_provider` value you pass at runtime.
The loader raises `ProviderEmptyContentError` on an empty file and `ValueError` when the top level is not a JSON object.

Three variables decide where the file is read from, in priority order: `--providers-json`, then `PROVIDERS_JSON_PATH`, then `.providers.json` in the current working directory.

A trimmed example from `.providers.json.example`:

```json
{
  "vsphere": {
    "type": "vsphere",
    "version": "<SERVER VERSION>",
    "fqdn": "SERVER FQDN/IP",
    "api_url": "<SERVER FQDN/IP>/sdk",
    "username": "USERNAME",
    "password": "PASSWORD",
    "guest_vm_linux_user": "LINUX VMS USERNAME",
    "guest_vm_linux_password": "LINUX VMS PASSWORD",
    "guest_vm_win_user": "WINDOWS VMS USERNAME",
    "guest_vm_win_password": "WINDOWS VMS PASSWORD",
    "luks_passphrase": "LUKS DISK ENCRYPTION PASSPHRASE",
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>",
    "endpoint_type": "vcenter"
  }
}
```

> **Warning:** `.providers.json.example` is only an example. The real `.providers.json` is loaded with `json.loads()`, so it must be strict JSON.
> Remove example comments before you use it.

Provider-specific expectations:

- `vsphere` needs `fqdn`, `api_url`, username/password, and usually guest credentials for post-migration validation.
- If your MTV deployment expects VDDK, include `vddk_init_image`. LUKS tests also read `luks_passphrase`.
- `ovirt` / RHV needs an API URL and username/password.
- `openstack` needs extra auth fields: `project_name`, `user_domain_name`, `region_name`, `user_domain_id`, and `project_domain_id`.
- `openshift` uses the local cluster as the source provider.
- `ova` expects `api_url` to point at the OVA source location.
- `hyperv` needs the provider fields plus `smb_url`, and optionally `smb_user` and `smb_password` when the SMB share uses different credentials.

> **Tip:** `mtv-api-tests generate` writes `.providers.json` for you with `0600` permissions, so the
> credentials never sit in a file you created by hand.

Post-migration validation also uses guest credentials from `.providers.json`. Linux checks read `guest_vm_linux_user` and `guest_vm_linux_password`.
Windows checks read `guest_vm_win_user` and `guest_vm_win_password`.

> **Note:** With the default `source_provider_insecure_skip_verify: "false"`, the suite validates source-provider TLS.
> vSphere and OpenStack provider setup fetch CA data from `fqdn:443`, and RHV always pulls a CA certificate.
> Use insecure provider connections only in lab environments where that is acceptable.

If you use OpenShift as the source provider, the suite expects OpenShift Virtualization template assets that match the hard-coded source VM fixture:

```python
create_and_store_resource(
    resource=VirtualMachineFromInstanceType,
    fixture_store=fixture_store,
    name=f"{vm_dict['name']}{vm_name_suffix}",
    namespace=namespace,
    client=client,
    instancetype_name="u1.small",
    preference_name="rhel.9",
    datasource_name="rhel9",
    storage_size="30Gi",
    additional_networks=[network_name],
)
```

That means an OpenShift-source lab needs:

- The `rhel9` `DataSource`.
- The `u1.small` `VirtualMachineClusterInstancetype`.
- The `rhel.9` `VirtualMachineClusterPreference`.

The helper also sets a `rhel` user with password `123456` via cloud-init and starts each VM it creates, so the suite can reach the guests over SSH.

## Storage prerequisites

The `storage_class` you choose must be a real, usable storage class for OpenShift Virtualization VM disks.
The suite validates that migrated disks land on that exact storage class after migration.

For standard cold and warm migration tests, that usually means:

- the storage class can provision PVCs for KubeVirt,
- the cluster can schedule VMs that use it,
- the storage class is available in the target cluster where the tests run.

Copy-offload adds stricter requirements:

- the source provider must be `vSphere`,
- the storage must be shared between vSphere and OpenShift,
- the target storage class must be block-backed,
- the environment must support `ReadWriteOnce` and `Block` volume mode for copy-offload mappings.

The example provider file shows the copy-offload fields the suite expects:

```json
{
  "vsphere-copy-offload": {
    "type": "vsphere",
    "version": "<SERVER VERSION>",
    "fqdn": "SERVER FQDN/IP",
    "api_url": "<SERVER FQDN/IP>/sdk",
    "username": "USERNAME",
    "password": "PASSWORD",
    "copyoffload": {
      "storage_vendor_product": "ontap",
      "datastore_id": "datastore-12345",
      "secondary_datastore_id": "datastore-67890",
      "non_xcopy_datastore_id": "datastore-99999",
      "default_vm_name": "rhel9-template",
      "storage_hostname": "storage.example.com",
      "storage_username": "admin",
      "storage_password": "your-password-here",
      "ontap_svm": "vserver-name",
      "esxi_clone_method": "ssh",
      "esxi_host": "your-esxi-host.example.com",
      "esxi_user": "root",
      "esxi_password": "your-esxi-password",
      "rdm_lun_uuid": "naa.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    }
  }
}
```

Supported copy-offload storage vendors are:

- `ontap`
- `vantara`
- `pureFlashArray`
- `powerflex`
- `powermax`
- `powerstore`
- `primera3par`
- `infinibox`
- `flashsystem`

`ontap`, `vantara`, `pureFlashArray`, `powerflex`, and `powermax` each need extra fields — one for all of them except `vantara`, which needs three. The other four need only the common fields:

| Vendor | Extra field |
| --- | --- |
| `ontap` | `ontap_svm` |
| `vantara` | `vantara_storage_id`, `vantara_storage_port`, `vantara_hostgroup_id_list` |
| `pureFlashArray` | `pure_cluster_prefix` |
| `powerflex` | `powerflex_system_id` |
| `powermax` | `powermax_symmetrix_id` |

Some copy-offload scenarios need extra fields:

- `secondary_datastore_id` for multi-datastore tests
- `non_xcopy_datastore_id` for mixed XCOPY/non-XCOPY tests
- `rdm_lun_uuid` for RDM disk tests, and `datastore_id` must point at a VMFS datastore for RDM to work
- `dedicated_migration_hosts` for the dedicated-migration-host scenario
- `resource_pool` to pin cloned VM placement, if auto-selection does not pick a compatible pool

> **Warning:** `datastore_id` and `secondary_datastore_id` must live on the same storage array and both support
> XCOPY/VAAI primitives, otherwise copy-offload silently falls back to the regular transfer path.

For array-specific Secret keys the vendor fields do not cover, add `storage_secret_extra` to the `copyoffload`
block. Keys must match the Forklift Secret `stringData` names, which are uppercase.

> **Warning:** Copy-offload is not an NFS scenario.
> The project’s copy-offload guide requires shared SAN/block storage and explicitly states that NFS is not supported for copy-offload.

If you set `storage_class` to `nfs`, the suite patches the cluster `nfs` `StorageProfile` to `ReadWriteOnce` and `Filesystem` before running.

> **Note:** That `StorageProfile` change is cluster-wide. Only use it in environments where changing the `nfs` profile is acceptable.

## Network prerequisites

Source VMs must have at least one NIC. The suite fails immediately if it cannot discover any network interfaces for the selected source VMs.

Network mapping works like this:

- the first source NIC is mapped to the OpenShift pod network,
- every additional source NIC is mapped to a `Multus` `NetworkAttachmentDefinition`.

That behavior comes directly from the network mapping helper:

```python
if pod_only or index == 0:
    _destination = {"type": "pod"}
else:
    _destination = {
        "name": nad_name,
        "namespace": multus_namespace,
        "type": "multus",
    }
```

The default `Multus` CNI config created by the suite is a bridge called `cnv-bridge`:

```python
bridge_type_and_name = "cnv-bridge"
config = {"cniVersion": "0.3.1", "type": f"{bridge_type_and_name}", "bridge": f"{bridge_type_and_name}"}
```

In practice:

- single-NIC migrations can run with the default pod-network mapping,
- multi-NIC migrations need `Multus` to be installed and working,
- the test account must be allowed to create `NetworkAttachmentDefinition` resources,
- if you use a custom `multus_namespace`, the account must be allowed to create or reuse NADs there.

## Optional scenario prerequisites

### Warm migration

Warm tests are written only for `vSphere` and `RHV`.

They also update the `ForkliftController` precopy interval, so the test account must be able to patch `forklift-controller` in the MTV namespace.

The comprehensive warm scenario is written around MTV 2.10+ features such as:

- static IP preservation,
- custom target VM namespace,
- PVC name templates,
- target labels,
- target affinity.

> **Note:** If you plan to run the comprehensive warm scenario, use an MTV version that already supports those features.

### Copy-offload

Copy-offload tests are `vSphere`-only. They also need:

- shared block storage visible from both vSphere and OpenShift,
- storage credentials,
- the copy-offload vendor-specific fields for your array,
- an ESXi clone method:
  - `ssh`, with `esxi_host`, `esxi_user`, `esxi_password`
  - or the default `vib` path, if your ESXi environment allows the required community VIB install

The copy-offload credential helper lets you supply sensitive values either in `.providers.json` or through environment variables, with environment variables taking precedence.

> **Tip:** Useful copy-offload overrides include `COPYOFFLOAD_STORAGE_HOSTNAME`, `COPYOFFLOAD_STORAGE_USERNAME`, `COPYOFFLOAD_STORAGE_PASSWORD`, `COPYOFFLOAD_ESXI_HOST`,
> `COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD`.

For MTV versions earlier than 2.11, copy-offload must already be enabled on the `ForkliftController`:

```yaml
spec:
  feature_copy_offload: 'true'
```

### LUKS-encrypted disks

`tests/luks/test_luks_cold_migration.py` is `vSphere`-only and marked `tier1`. It needs:

- a source VM whose disks are LUKS-encrypted,
- a LUKS passphrase, either in the provider field `luks_passphrase`, or overridden per VM with a `luks_passphrase` key inside that VM's `virtual_machines` entry,
- one of the two built-in plans, `test_luks_cold_migration` or `test_luks_cold_migration_wrong_key`.

> **Note:** The wrong-key plan deliberately supplies a bad passphrase and expects the migration to fail. It is a
> negative test, not a broken lab.

### XFS source and target disks

`tests/cold/test_cold_migration_xfs.py` and `tests/warm/test_warm_migration_xfs.py` are `vSphere`-only and marked `tier1`. They need:

- an XFS-formatted source VM matching `test_cold_migration_xfs` or `test_warm_migration_xfs`,
- `xfs_compatibility: True` in the plan, plus an `xfs_check` block naming the on-guest command, mount point, and expected output,
- SSH access to the migrated guest, because the XFS check runs inside the VM.

The built-in plans check `crc=0` from `/usr/sbin/xfs_info` on `/` and on a secondary partition such as `/sdb1`.

### Deep Inspection

`tests/deep_inspection/` is `vSphere`-only and marked `deep_inspection`. It runs MTV deep inspection / Conversion CRs and needs:

- a source VM matching `test_standalone_di_vsphere` or `test_warm_di_concerns`,
- the deep-inspection feature available in your MTV version,
- an MTV version new enough to surface the capture results the suite asserts on.

### CA certificate field

`tests/cold/test_ca_crt_migration.py` is marked `ca_crt` and `tier1`.
It is skipped at collection time for `openshift` and `ova` providers, because those provider types do not carry a `ca.crt` field in their provider secrets.

### Shared disks

`tests/shared_disk/` is `vSphere`-only and marked `shared_disk`.
Both plans need paired source VMs: one with `migrate_shared_disks: True` and one with `migrate_shared_disks: False`, so the suite can verify that shared-disk policy is honored.

### Hooks and AAP

`tests/hooks/` covers post-hook failure handling and AAP (Ansible Automation Platform) hook integration.
The AAP test is `vSphere`-only and needs a reachable AWX/AAP instance, including its API token and a project with the pre-hook and post-hook job templates.
Hook scenarios also leave a failed migration behind on purpose; use `--skip-teardown` when you want to inspect it.

### MTV operator upgrade

`tests/upgrade/test_upgrade_migration.py` is marked `upgrade` and `vSphere`-only. It clones the `mtv-autodeploy` repository at test time and runs its upgrade script, so it needs:

- `git` available where pytest runs,
- `--tc=upgrade_repo_url:<url>`, `--tc=upgrade_repo_ref:<ref>`, and `--tc=upgrade_script_path:<relative-path>`; the fixture raises `ValueError` if any of the three is missing,
- outbound network access to clone that repository,
- a cluster you are willing to upgrade, since the operator is left on the newer version after the test.

> **Warning:** This is the only scenario that mutates the cluster beyond per-test namespaces, and the only one that
> needs config keys that are not in `tests/tests_config/config.py`. Do not run it on a shared cluster.

### Copy-offload snapshots

`tests/copyoffload/test_copyoffload_migration.py` contains snapshot scenarios marked `copyoffload_snapshots`.
They need a vSphere provider with XCOPY-capable storage and `snapshots: 2` set on the plan VM, which the suite creates and deletes on the source.

### Remote scenarios

Tests marked `remote` are skipped unless `remote_ocp_cluster` is set.

In the current fixture implementation, that value is also validated against the connected cluster host string, so set it deliberately and make sure it matches your target
environment naming.

### Scheduling and labeling scenarios

The comprehensive cold scenario can:

- label a worker node,
- use `target_node_selector`,
- apply `target_labels`,
- apply `target_affinity`,
- create or use a custom `vm_target_namespace`.

Those scenarios need:

- at least one worker node,
- permission to patch `Node` resources,
- permission to create resources in any custom target namespace.

The node-selection helper prefers Prometheus metrics, but it falls back to the first worker node if monitoring access is not available.

## Provider-type skipping

The suite decides which tests apply at collection time, by reading the `type` field of the provider you selected in `.providers.json`:

- warm migration tests are skipped for `openstack`, `openshift`, `ova`, and `hyperv`,
- tests marked `copyoffload`, `shared_disk`, `deep_inspection`, `aap`, or `luks` are skipped for any non-vSphere provider,
- `ca_crt` tests are skipped for `openshift` and `ova`.

This means you can point a full run at one provider without hand-picking tests.

## Permission prerequisites

The easiest lab setup is `cluster-admin`.
In shared environments, least-privilege RBAC is possible, but it still needs to cover the resources the suite actually creates, patches, reads, and deletes.

At minimum, the account running the suite should be able to:

- Create and delete `Namespace` resources.
- Create, read, update, and delete `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Migration`, and `Hook` resources in the test namespace.
- Create, read, and delete `Secret`, `NetworkAttachmentDefinition`, `Pod`, and `VirtualMachine` resources in the test namespace.
- Read `Pod` resources in the MTV namespace so the suite can verify `forklift-*` health.
- Patch `ForkliftController` in the MTV namespace for warm-migration scenarios.
- Read `StorageClass` resources and patch `StorageProfile` if you use the `nfs` storage path.
- Read `ClusterVersion`.
- List and patch `Node` resources if you run the scheduling tests.
- Create resources in any custom `vm_target_namespace` or `multus_namespace` you configure.
- Update the MTV operator subscription if you run the `upgrade` scenario.
- Read and manage `Node` objects in the destination cluster for the worker-node labeling fixtures.

Source-side permissions matter too. Depending on the provider and scenario, the suite may also need to:

- read inventory,
- clone source VMs,
- power source VMs on or off,
- delete cloned VMs during cleanup,
- create or delete snapshots,
- attach extra test disks.

> **Warning:** Cleanup is best-effort, not a guarantee.
> If the run is interrupted, if you use `--skip-teardown`, or if the test account cannot clean up provider-side clones or snapshots, test artifacts can remain behind.

## Secure configuration handoff

If you run the suite in-cluster as an OpenShift `Job`, the supported path is to let the CLI generate the manifest for you:

```bash
uv run mtv-api-tests generate
uv run mtv-api-tests run --mode job
```

`generate` writes a self-contained `mtv-api-tests-manifests.yaml` holding a `Namespace`, a `Secret`, and a `Job`, with `0600` permissions.
The Secret carries `providers.json`, `cluster_host`, `cluster_username`, `cluster_password`, and `cluster_verify_ssl`, all base64 encoded.

If you prefer to build the Secret yourself, this is the same minimum set:

```bash
oc create namespace mtv-tests

read -sp "Enter cluster password: " CLUSTER_PASSWORD && echo
oc create secret generic mtv-test-config \
  --from-file=providers.json=.providers.json \
  --from-literal=cluster_host=https://api.your-cluster.com:6443 \
  --from-literal=cluster_username=kubeadmin \
  --from-literal=cluster_password="${CLUSTER_PASSWORD}" \
  -n mtv-tests
unset CLUSTER_PASSWORD
```

That example is useful even if you do not use `Job`s, because it shows the minimum configuration you need to have ready:

- `.providers.json`
- `cluster_host`
- `cluster_username`
- `cluster_password`

You still need two more runtime values for an actual test run:

- `source_provider`
- `storage_class`

> **Warning:** `generate` bakes credentials into `mtv-api-tests-manifests.yaml`. It is listed in `.dockerignore`
> and `.gitignore`; do not commit it.

If all of the prerequisites above are in place, the suite can create its migration resources, run real MTV workflows, validate the results, and clean up in the way the codebase
expects.
