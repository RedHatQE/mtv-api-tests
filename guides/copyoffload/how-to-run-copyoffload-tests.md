# How to Run MTV API Tests

This guide gets you from zero to running tests. The `mtv-api-tests` CLI handles
configuration discovery and test execution, so you don't need to manually write
JSON config files or Job manifests.

---

## Prerequisites

- **OpenShift cluster** with MTV and CNV installed
- **vSphere environment** with a test VM (SSH access, guest agent installed)
- **`uv`** installed ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- **`oc`** CLI logged into your cluster

For copy-offload tests specifically, you also need shared block storage (SAN/iSCSI/FC)
between vSphere and OpenShift. See [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers).

For full prerequisite details, see the [README](../../README.md#prerequisites).

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/RedHatQE/mtv-api-tests.git
cd mtv-api-tests
uv sync
```

### 2. Generate configuration

```bash
uv run mtv-api-tests generate
```

The wizard connects to your vSphere and OpenShift environments and walks you through:

1. vSphere credentials (or reads `VSPHERE_HOST`, `VSPHERE_USERNAME`, `VSPHERE_PASSWORD` env vars)
2. Datastore, test VM, and ESXi host selection
3. Storage vendor and credentials (for copy-offload)
4. OpenShift storage class selection
5. Test category (`all`, `tier0`, `copyoffload`, `warm`, `remote`)

It writes two files:

- **`.providers.json`** -- provider connection details and test parameters
- **`mtv-api-tests-manifests.yaml`** -- ready-to-apply Namespace + Secret + Job manifest

### 3. Run tests

```bash
# Run locally (uses your current oc session or prompts for credentials)
uv run mtv-api-tests run --mode local

# Or run as an OpenShift Job
uv run mtv-api-tests run --mode job
```

That's it. The CLI resolves the source provider, storage class, and cluster
credentials from the generated config, or prompts if anything is missing.

---

## Running Specific Tests

### By category

```bash
uv run mtv-api-tests run --mode local --category tier0
uv run mtv-api-tests run --mode local --category copyoffload
uv run mtv-api-tests run --mode local --category warm
```

### By test name

```bash
uv run mtv-api-tests run --mode local -k test_copyoffload_thin_migration
uv run mtv-api-tests run --mode local -k "thin or thick"
```

### Non-interactive (CI-friendly)

All prompts can be bypassed with flags:

```bash
uv run mtv-api-tests run --mode local \
  --category copyoffload \
  --source-provider vsphere-8.0.3.00400 \
  --storage-class ontap-san-block \
  -k test_copyoffload_thin_migration
```

### List available tests

```bash
uv run pytest --collect-only -q -m copyoffload
uv run pytest --collect-only -q -m tier0
```

---

## Running as an OpenShift Job

The `generate` command creates a self-contained manifest. Deploy it with:

```bash
uv run mtv-api-tests run --mode job
```

This runs `oc apply -f mtv-api-tests-manifests.yaml`, which creates a unique
namespace, secret, and Job.

**Follow logs:**

```bash
# The CLI prints the namespace and job name after deployment
oc logs -f -n <namespace> job/<job-name>
```

**Check status:**

```bash
oc get jobs -n <namespace>
```

**Retrieve JUnit report:**

```bash
POD=$(oc get pods -n <namespace> -l job-name=<job-name> -o jsonpath='{.items[0].metadata.name}')
oc cp <namespace>/$POD:/app/junit-report.xml ./junit-report.xml
```

---

## Copy-Offload: What's Different

Copy-offload uses the storage array to move VM disk data directly (via XCOPY/VAAI),
bypassing the standard VDDK transfer. The test suite handles the setup automatically --
creating storage secrets, configuring the StorageMap with `offloadPlugin`, and managing
the ESXi clone method.

**Extra requirements:**

- Shared block storage between vSphere and OpenShift (same physical array)
- VAAI enabled on ESXi hosts
- Clone method: SSH (recommended) or VIB
- For MTV < 2.11: enable the feature gate in ForkliftController spec

The `generate` wizard collects all of this interactively. For manual configuration
details, see [Copy-Offload Migrations](../../docs/copy-offload-migrations.md).

**Recommended first test:**

```bash
uv run mtv-api-tests run --mode local --category copyoffload -k test_copyoffload_thin_migration
```

---

## Troubleshooting

**Storage connection errors:**

```bash
oc get pods -n <csi-driver-namespace>          # CSI driver running?
oc logs -n openshift-mtv deployment/forklift-controller --tail=50
```

**Clone method (SSH):**

```bash
ssh root@<esxi-host>                           # SSH access works?
```

**Clone method (VIB):**

```bash
oc logs -n openshift-mtv -l app=vsphere-xcopy-volume-populator --tail=50
```

**Migration stuck:**

```bash
oc get plan -n openshift-mtv -o wide
oc logs -n openshift-mtv deployment/forklift-controller -f
```

**Debug flags:** Add `--skip-teardown` to keep resources after test failure, or
use `-s -vv` for verbose pytest output. See [README: Useful Test Options](../../README.md#useful-test-options).

---

## Additional Resources

- [MTV Documentation](https://access.redhat.com/documentation/en-us/migration_toolkit_for_virtualization/)
- [Copy-Offload Feature Documentation](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator)
- [Clone Methods Guide](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#clone-methods-vib-vs-ssh)
- [Supported Storage Providers](https://github.com/kubev2v/forklift/tree/main/cmd/vsphere-xcopy-volume-populator#supported-storage-providers)
