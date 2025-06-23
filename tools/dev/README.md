# MTV API Test Development Tools (`dev-tools.sh`)

A collection of tools for managing OpenShift clusters for MTV (Migration Toolkit for Virtualization) testing and development. This script simplifies common tasks like logging in, running tests, and managing Ceph storage.

## Installation

1. Make the script executable:

    ```bash
    chmod +x tools/dev/dev-tools.sh
    ```

2. (Optional but Recommended) Install `xsel` to enable the copy-to-clipboard feature for passwords.

    ```bash
    # On Fedora/CentOS
    sudo dnf install xsel
    # On Debian/Ubuntu
    sudo apt-get install xsel
    ```

3. (Optional but Recommended) Add tab completion for your shell. This will enable auto-completion for commands, cluster names, and command-specific flags.

    Add the appropriate line to your shell's startup file (`~/.bashrc` for Bash, `~/.zshrc` for Zsh).

    **For Bash:**

    ```bash
    source <(/path/to/mtv-api-tests/tools/dev/dev-tools.sh generate-completion-script bash)
    ```

    **For Zsh:**

    ```bash
    source <(/path/to/mtv-api-tests/tools/dev/dev-tools.sh generate-completion-script zsh)
    ```

    *Note: Remember to replace `/path/to/mtv-api-tests` with the actual path to your project directory.*

    After adding the line, restart your shell or run `source ~/.bashrc` / `source ~/.zshrc`.

## Usage

```bash
./tools/dev/dev-tools.sh <command> [arguments]
```

### Global Options

* `--help`: Show the help message.

---

## Commands

### `list-clusters`

Lists available test clusters.

* **Default:** Shows a summary view with cluster name, OCP version, and MTV version.

    ```bash
    ./tools/dev/dev-tools.sh list-clusters
    # qemtv-01             OCP: 4.19.0-rc.5   MTV: 2.9.0 (redhat-osbs-...)
    # qemtv-02             OCP: 4.19.0-rc.5   MTV: 2.9.0 (redhat-osbs-...)
    ```

* **With `--full`:** Shows detailed information for each cluster.

    ```bash
    ./tools/dev/dev-tools.sh list-clusters --full
    ```

### `cluster-password <cluster-name>`

Prints the `kubeadmin` password for a cluster and copies it to the clipboard.

```bash
./tools/dev/dev-tools.sh cluster-password qemtv-01
```

### `cluster-login <cluster-name>`

Logs into a cluster, prints its details, and copies the password to the clipboard.

```bash
./tools/dev/dev-tools.sh cluster-login qemtv-01
```

### `run-tests <cluster-name> [test-args...]`

Runs the MTV API tests against a specified cluster. This command constructs a `pytest` command based on your arguments.

You can either use a pre-defined test suite or specify the provider and storage type manually. Any additional arguments are passed directly to `pytest`.

**Pre-defined Test Suites:**
The following are shortcuts for common test configurations.

* `vmware6-csi`
* `vmware6-csi-remote`
* `vmware7-ceph`
* `vmware7-ceph-remote`
* `vmware8-nfs`
* `vmware8-ceph-remote`
* `vmware8-csi`
* `openstack-ceph`
* `openstack-csi`
* `ovirt-ceph`
* `ovirt-csi`
* `ovirt-csi-remote`
* `ova-ceph`

*Example:*

```bash
# Run the pre-defined vmware7-ceph test suite
./tools/dev/dev-tools.sh run-tests qemtv-01 vmware7-ceph

# Run the same suite, but only the tests in a specific file
./tools/dev/dev-tools.sh run-tests qemtv-01 vmware7-ceph tests/test_mtv_cold_migration.py
```

**Custom Test Runs:**
You can also define a custom run using flags.

* `--provider=<provider>`: `vmware6`, `vmware7`, `vmware8`, `ovirt`, `openstack`, `ova`
* `--storage=<storage>`: `ceph`, `nfs`, `csi`
* `--remote`: Add this flag for remote cluster tests.
* `--data-collect`: Add this flag to enable the data collector for failed tests.

*Example:*

```bash
# Custom run for openstack with ceph storage
./tools/dev/dev-tools.sh run-tests qemtv-01 --provider=openstack --storage=ceph

# Custom run for a remote ovirt cluster with csi storage
./tools/dev/dev-tools.sh run-tests qemtv-02 --provider=ovirt --storage=csi --remote
```

### `mtv-resources <cluster-name>`

Lists MTV-related resources (pods, plans, migrations, etc.) in a cluster.

```bash
./tools/dev/dev-tools.sh mtv-resources qemtv-01
```

### `ceph-df <cluster-name> [--watch]`

Displays Ceph cluster storage usage.

* **Default:** Shows current usage.

    ```bash
    ./tools/dev/dev-tools.sh ceph-df qemtv-01
    ```

* **With `--watch`:** Monitors usage in real-time, refreshing every 10 seconds.

    ```bash
    ./tools/dev/dev-tools.sh ceph-df qemtv-01 --watch
    ```

### `ceph-cleanup <cluster-name> [--execute]`

Generates commands to clean up Ceph resources (RBD images, snapshots, and trash).

* **Default:** Prints the commands to be run and copies them to the clipboard.

    ```bash
    ./tools/dev/dev-tools.sh ceph-cleanup qemtv-01
    ```

* **With `--execute`:** Prompts for confirmation and then runs the cleanup commands. It will continue even if some commands fail.

    ```bash
    ./tools/dev/dev-tools.sh ceph-cleanup qemtv-01 --execute
    ```

### `csi-nfs-df <cluster-name>`

Checks the available space on the NFS CSI driver.

```bash
./tools/dev/dev-tools.sh csi-nfs-df qemtv-01
```
