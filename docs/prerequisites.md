# Prerequisites

`mtv-api-tests` runs live end-to-end migrations. It talks to a real OpenShift cluster, a real MTV installation, and a real source provider. Before you run it, make sure the target cluster, source environment, storage, network, and RBAC are ready for destructive integration testing.

> **Warning:** Run this suite only in a lab or other disposable environment. The tests create and delete `Namespace`, `Provider`, `StorageMap`, `NetworkMap`, `Plan`, `Migration`, `Hook`, `Secret`, `NetworkAttachmentDefinition`, `Pod`, and `VirtualMachine` resources. Some scenarios also clone source VMs, change source power state, add disks, and create snapshots.

Example test definitions in `tests/tests_config/config.py` show that clearly:

```python
"test_copyoffload_thin_snapshots_migration": {
    "virtual_machines": [
        {
            "name": "xcopy-template-test",
            "source_vm_power": "off",
            "guest_agent": True,
            "clone": True,
            "disk_type": "thin",
            "snapshots": 2,
        },
    ],
    "warm_migration": False,
    "copyoffload": True,
},
```

Choose source VMs that match the scenario you plan to run. Do not point the suite at production VMs or production-only datastores.

## Minimum environment

Every run needs all of the following:

- A reachable OpenShift API endpoint.
- Valid OpenShift credentials for the test runner.
- MTV installed and healthy in `openshift-mtv`, unless you intentionally override `mtv_namespace`.
- OpenShift Virtualization installed on the destination cluster.
- A usable destination `storage_class`.
- A `source_provider` value that matches a key in `.providers.json`.

The cluster client is created from these settings:

```python
def get_cluster_client() -> DynamicClient:
    host = get_value_from_py_config("cluster_host")
    username = get_value_from_py_config("cluster_username")
    password = get_value_from_py_config("cluster_password")
    insecure_verify_skip = get_value_from_py_config("insecure_verify_skip")
    _client = get_client(host=host, username=username, password=password, verify_ssl=not insecure_verify_skip)
```

The built-in test config also defines the key global defaults:

```python
insecure_verify_skip: str = "true"
source_provider_insecure_skip_verify: str = "false"
mtv_namespace: str = "openshift-mtv"
remote_ocp_cluster: str = ""
snapshots_interval: int = 2
plan_wait_timeout: int = 3600
```

> **Note:** `insecure_verify_skip` controls TLS verification for the OpenShift API connection. `source_provider_insecure_skip_verify` is separate and defaults to `false`, so source-provider certificate verification is on unless you change it.

Before tests start, the suite checks `forklift-*` pods in the MTV namespace and fails if the `forklift-controller` pod is missing or any `forklift` pod is not healthy.

## Source provider configuration

The code supports these source provider types:

| Source provider | Cold migration | Warm migration | Copy-offload |
| --- | --- | --- | --- |
| `vsphere` | Yes | Yes | Yes |
| `ovirt` / RHV | Yes | Yes | No |
| `openstack` | Yes | No | No |
| `openshift` | Yes | No | No |
| `ova` | Yes | No | No |

Your `.providers.json` file must exist, must be non-empty, and must contain a key that exactly matches the `source_provider` value you pass at runtime.

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
    "vddk_init_image": "<PATH TO VDDK INIT IMAGE>"
  }
}
```

> **Warning:** `.providers.json.example` is only an example. The real `.providers.json` is loaded with `json.loads()`, so it must be strict JSON. Remove example comments before you use it.

Provider-specific expectations:

- `vsphere` needs `fqdn`, `api_url`, username/password, and usually guest credentials for post-migration validation. If your MTV deployment expects VDDK, include `vddk_init_image`.
- `ovirt` / RHV needs an API URL and username/password.
- `openstack` needs extra auth fields: `project_name`, `user_domain_name`, `region_name`, `user_domain_id`, and `project_domain_id`.
- `openshift` uses the local cluster as the source provider.
- `ova` expects `api_url` to point at the OVA source location.

Post-migration validation also uses guest credentials from `.providers.json`. Linux checks read `guest_vm_linux_user` and `guest_vm_linux_password`. Windows checks read `guest_vm_win_user` and `guest_vm_win_password`.

> **Note:** With the default `source_provider_insecure_skip_verify: "false"`, the suite validates source-provider TLS. vSphere and OpenStack provider setup fetch CA data from `fqdn:443`, and RHV always pulls a CA certificate. Use insecure provider connections only in lab environments where that is acceptable.

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

## Storage prerequisites

The `storage_class` you choose must be a real, usable storage class for OpenShift Virtualization VM disks. The suite validates that migrated disks land on that exact storage class after migration.

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
- `primera3par`
- `pureFlashArray`
- `powerflex`
- `powermax`
- `powerstore`
- `infinibox`
- `flashsystem`

Some copy-offload scenarios need extra fields:

- `secondary_datastore_id` for multi-datastore tests
- `non_xcopy_datastore_id` for mixed XCOPY/non-XCOPY tests
- `rdm_lun_uuid` for RDM disk tests

> **Warning:** Copy-offload is not an NFS scenario. The project’s copy-offload guide requires shared SAN/block storage and explicitly states that NFS is not supported for copy-offload.

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

> **Tip:** Useful copy-offload overrides include `COPYOFFLOAD_STORAGE_HOSTNAME`, `COPYOFFLOAD_STORAGE_USERNAME`, `COPYOFFLOAD_STORAGE_PASSWORD`, `COPYOFFLOAD_ESXI_HOST`, `COPYOFFLOAD_ESXI_USER`, and `COPYOFFLOAD_ESXI_PASSWORD`.

For MTV versions earlier than 2.11, copy-offload must already be enabled on the `ForkliftController`:

```yaml
spec:
  feature_copy_offload: 'true'
```

### Remote scenarios

Tests marked `remote` are skipped unless `remote_ocp_cluster` is set.

In the current fixture implementation, that value is also validated against the connected cluster host string, so set it deliberately and make sure it matches your target environment naming.

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

## Permission prerequisites

The easiest lab setup is `cluster-admin`. In shared environments, least-privilege RBAC is possible, but it still needs to cover the resources the suite actually creates, patches, reads, and deletes.

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

Source-side permissions matter too. Depending on the provider and scenario, the suite may also need to:

- read inventory,
- clone source VMs,
- power source VMs on or off,
- delete cloned VMs during cleanup,
- create or delete snapshots,
- attach extra test disks.

> **Warning:** Cleanup is best-effort, not a guarantee. If the run is interrupted, if you use `--skip-teardown`, or if the test account cannot clean up provider-side clones or snapshots, test artifacts can remain behind.

## Secure configuration handoff

If you run the suite in-cluster as an OpenShift `Job`, the project already includes a working secret example that packages the core prerequisites:

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

If all of the prerequisites above are in place, the suite can create its migration resources, run real MTV workflows, validate the results, and clean up in the way the codebase expects.
