# MTV API Test Developer Tool: mtv-dev

This directory contains `mtv-dev`, a Go command-line tool to simplify interaction with MTV (Migration Toolkit for
Virtualization) test clusters. It provides complete feature parity with the legacy scripts while offering improved
performance, reliability, and an interactive Text User Interface (TUI).

## Key Features

- **📟 Interactive TUI**: Beautiful terminal interface for cluster management
- **⚡ High Performance**: Concurrent operations and efficient cluster processing
- **🎯 Complete CLI**: Full command-line interface with tab completion
- **✅ Comprehensive Testing**: Extensive test coverage for all functionality
- **🔄 Manual Refresh**: On-demand cluster status updates and selective refresh

The tool is built from multiple well-organized Go files:

- `main.go` - Entry point and command initialization
- `types.go` - Type definitions and constants  
- `client.go` - Kubernetes/OpenShift client operations
- `commands.go` - All command implementations
- `completion.go` - Tab completion functions
- `helpers.go` - Utility functions
- `tui/` - Interactive Text User Interface components

---

## Quick Start

1. **Build the tool:**

   ```bash
   cd tools/dev
   make build
   ```

2. **Launch the TUI (recommended):**

   ```bash
   ./mtv-dev tui
   ```

3. **Or use individual commands:**

   ```bash
   ./mtv-dev list-clusters --full
   ./mtv-dev cluster-login qemtv-01
   ./mtv-dev run-tests qemtv-02 vmware8-ceph-remote
   ./mtv-dev get-iib 2.9
   ./mtv-dev get-vms vsphere-7.0.3
   ```

4. **Enable tab completion:**

   ```bash
   source <(./mtv-dev completion bash)  # For Bash
   source <(./mtv-dev completion zsh)   # For Zsh
   ```

---

## Prerequisites

1. **Red Hat Network Access:** You must be connected to the Red Hat internal network (VPN) to access the cluster credentials NFS share.
2. **Go:** A recent version of the Go toolchain must be installed to build the tool.
3. **`oc` CLI:** The OpenShift CLI (`oc`) must be in your system's `PATH`. The tool uses it for login operations.
4. **NFS Mount:** The tool requires access to cluster credentials stored on an NFS share. It automatically mounts the NFS share at `/mnt/cnv-qe.rhcloud.com` if not already available (requires sudo privileges for mounting).
5. **Clipboard Utility:** For the `cluster-login` and `cluster-password` commands to copy the password to the clipboard, a clipboard utility like `xsel` or `xclip` (for Linux) is required.

## Build Instructions

Navigate to this directory and build using either Make or Go directly:

### Using Make (Recommended)

```bash
cd tools/dev
make install    # Build the binary
# or
make all        # Same as 'make install'
```

### Using Go directly

```bash
cd tools/dev
go build -o mtv-dev .
```

Both methods create a binary named `mtv-dev` in the current directory.

## Interactive TUI (Text User Interface)

The tool features a modern, interactive TUI for cluster management. Launch it with:

```bash
./mtv-dev tui
```

### TUI Features

- **📊 Cluster Dashboard**: Organized cluster status display with manual refresh capabilities
- **🎯 Dual-pane Interface**: Cluster list on the left (30%), detailed info on the right (70%)
- **⚡ Fast Navigation**: Keyboard shortcuts for efficient cluster management
- **🔄 Manual Refresh**: Refresh all clusters (Ctrl+R) or single cluster (Ctrl+U)
- **🔍 Search**: Quick cluster filtering with `/` key
- **📋 Copy Integration**: Copy cluster details, passwords, and login commands to clipboard
- **🎨 Color-coded Status**: Visual indicators for cluster accessibility and health
- **📦 IIB Build Browser**: Interactive interface to browse and copy Forklift FBC builds by MTV version
- **🎨 Theme Selection**: Multiple themes (Dark, Light, Blue, Neon, Classic Light) with live switching
- **🔧 Smart Filtering**: Dynamic OCP version filtering based on available builds for selected environment

### TUI Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `↑/↓` | Navigate lists and items |
| `Tab/Shift+Tab` | Switch between panes (universal across all screens) |
| `Enter` | Copy selected field to clipboard / Expand tree |
| `←/→` | Collapse/Expand tree (in provider screen) |
| `/` | Search/filter clusters |
| `Ctrl+R` | Refresh all clusters |
| `Ctrl+U` | Refresh selected cluster |
| `Esc` | Go back / Exit search |
| `q` or `Ctrl+C` | Quit |

### TUI Display

The TUI provides a clean, organized view:

**Left Pane (Cluster List):**

- Cluster names with status indicators
- Current accessibility status (✅ Online, ❌ Offline, ⏰ Timeout)

**Right Pane (Cluster Details):**

- OpenShift version and console URL
- MTV and CNV version information  
- IIB (Index Image Bundle) details
- kubeadmin password and login command
- Copy any field with Enter key

### IIB Builds Interface

The TUI includes a dedicated interface for browsing Forklift FBC (File-Based Catalog) builds:

**Features:**

- Interactive MTV version input with validation
- Real-time build data from kuflox cluster
- Three-pane layout: Build Types (prod/stage) | OCP Versions | Build Details
- Dynamic OCP version filtering based on selected build type
- One-click IIB copy to clipboard
- Automatic kuflox authentication with SSO support

**Navigation:**

- Enter MTV version (e.g., "2.9") and press Enter
- Use Tab/Shift+Tab or arrow keys to navigate between panes
- Press Enter to copy selected IIB build to clipboard
- Press Esc to go back to previous screen

### Theme Selection

Choose from multiple professionally designed themes:

- **Dark** (default): Elegant dark theme with muted colors
- **Light**: Clean light theme with high contrast
- **Classic Light**: Traditional light theme with pure black text
- **Blue**: GitHub-inspired dark blue theme
- **Neon**: Retro terminal theme with bright accent colors

Access via main menu → "🎨 Themes" → Select theme → Press Enter to apply

## Installation

### Option 1: Create a Soft Link (Recommended)

Create a soft link to the binary in your local bin directory:

```bash
mkdir -p ~/.local/bin
ln -sf $(pwd)/mtv-dev ~/.local/bin/mtv-dev
```

Make sure `~/.local/bin` is in your `PATH`. Add this to your shell profile if needed:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc  # For Bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # For Zsh
```

### Option 2: System-wide Installation

Move the binary to a system-wide location:

```bash
sudo mv mtv-dev /usr/local/bin
```

### Option 3: Run Directly

You can also run it directly from the current directory:

```bash
./mtv-dev
```

## Testing

The project has comprehensive test coverage across multiple test suites:

- **Main package tests** (`main_test.go`) - CLI commands, validation, error handling, IIB functionality
- **TUI tests** (`tui/models_test.go`) - Interface, interaction, state management, IIB functionality, theme system

### Quick Test Commands

```bash
# Run all tests with coverage summary
make test

# Run specific test suites
make test-main      # Run only CLI/main package tests
make test-tui       # Run only TUI tests

# Verbose output for debugging
make test-verbose

# Generate HTML coverage report
make test-coverage

# Pretty output with gotestsum
make test-pretty
```

### Test Structure

The tests are organized following Go best practices:

```text
tools/dev/
├── main_test.go              # Main package tests (CLI functionality)
│   ├── Command validation tests
│   ├── Error handling tests  
│   ├── Flag and argument tests
│   ├── IIB functionality tests (get-iib command)
│   ├── Mock dependency tests
│   └── Integration tests
└── tui/
    └── models_test.go        # TUI package tests (interface functionality)
        ├── Model initialization tests
        ├── Message handling tests
        ├── View rendering tests
        ├── Key binding tests
        ├── IIB TUI functionality tests
        ├── Theme system tests
        ├── Performance tests
        └── Error handling tests
```

### Advanced Testing

```bash
# Manual test execution
go test -v ./...                           # All tests with verbose output
go test -v ./tui/                         # TUI tests only
go test -v .                              # Main package tests only
go test -coverprofile=coverage.out ./... # Run with coverage
go tool cover -html=coverage.out         # View HTML coverage report

# Clean up test artifacts
make clean
```

### Test Features

- **Mock Dependencies**: Comprehensive mocking system for isolated testing
- **Performance Tests**: Validate TUI responsiveness under load
- **Error Scenarios**: Robust error handling and edge case testing
- **Integration Tests**: End-to-end workflow validation
- **Coverage Tracking**: Detailed test coverage reporting

## Tab Completion

The tool supports comprehensive tab completion for commands, flags, and arguments:

- **Commands**: Tab completion for all available commands
- **Cluster names**: Auto-completion for cluster names (e.g., `qemtv-01`, `qemtvd-02`)
- **Provider types**: Completion for `--provider` flag (vmware6, vmware7, vmware8, ovirt, openstack, ova)
- **Storage types**: Completion for `--storage` flag (ceph, nfs, csi)  
- **Template names**: Completion for run-tests templates (vmware8-ceph-remote, etc.)
- **MTV versions**: Completion for get-iib MTV version argument (2.9, etc.)
- **Shell types**: Completion for completion script generation (bash, zsh)
- **Output formats**: Completion for `--output` flag (table, json, simple)
- **Provider names**: Dynamic completion for get-vms provider names (loaded from config)

**Examples of tab completion in action:**

```bash
./mtv-dev run-tests <TAB>           # Shows available cluster names
./mtv-dev run-tests qemtv-01 <TAB>  # Shows available templates
./mtv-dev run-tests qemtv-01 --provider <TAB>   # Shows: vmware6 vmware7 vmware8 ovirt openstack ova
./mtv-dev run-tests qemtv-01 --storage <TAB>    # Shows: ceph csi nfs
./mtv-dev get-iib <TAB>             # Shows available MTV versions: 2.9
./mtv-dev cluster-password <TAB>    # Shows available cluster names
```

### Enable Tab Completion

**For Bash:**

```bash
source <(./mtv-dev completion bash)
# Or add to your ~/.bashrc:
echo 'source <(mtv-dev completion bash)' >> ~/.bashrc
```

**For Zsh:**

```bash
source <(./mtv-dev completion zsh)
# Or add to your ~/.zshrc:
echo 'source <(mtv-dev completion zsh)' >> ~/.zshrc
```

**For Fish:**

```bash
./mtv-dev completion fish | source
# Or save to file:
./mtv-dev completion fish > ~/.config/fish/completions/mtv-dev.fish
```

**For PowerShell:**

```powershell
./mtv-dev completion powershell | Out-String | Invoke-Expression
```

## Features

- **🖥️ Interactive TUI**: Modern terminal interface for cluster management and IIB build browsing
- **⚡ High Performance**: Concurrent cluster processing and efficient operations
- **🎯 Complete CLI**: Full command-line interface with comprehensive tab completion
- **🔄 Manual Refresh**: On-demand cluster updates and selective refresh capabilities
- **📋 Clipboard Integration**: Copy passwords, login commands, cluster details, and IIB builds
- **🎨 Color-coded Output**: Visual indicators for success (green), errors (red), warnings (yellow)
- **🗂️ Automatic NFS Mounting**: Seamless cluster credentials NFS share management
- **🔧 Automatic Tool Setup**: Ceph commands automatically enable required cluster tools
- **📦 IIB Build Retrieval**: Extract latest Forklift FBC builds from kuflox cluster with SSO support
- **🎨 Theme System**: Multiple UI themes with live switching and consistent styling
- **✅ Comprehensive Testing**: Extensive test coverage ensuring reliability and stability

## Command Reference

For a full list of commands and options, run:

```bash
./mtv-dev --help
```

For detailed options on any command, run:

```bash
./mtv-dev <command> --help
```

Most commands require a `<cluster-name>` argument (e.g., `qemtv-01`).

### Commands

- **`tui`**: Launch the interactive Text User Interface for cluster management.
  - Provides cluster management and monitoring in a beautiful terminal interface.
  - Features dual-pane layout with keyboard navigation and clipboard integration.
  - Supports manual refresh, search, and detailed cluster information display.

- **`list-clusters`**: List all available clusters (processes clusters concurrently for speed).
  - `--full`: Show full details for each cluster (OCP, MTV, CNV, IIB, etc.).
  - `--verbose`: Show detailed error information for failed clusters.

- **`cluster-password <cluster-name>`**: Get the kubeadmin password for a cluster.
  - `--no-copy`: Do not copy the password to the clipboard (default is to copy).

- **`cluster-login <cluster-name>`**: Display login command and cluster info in a tree format.
  - `--no-copy`: Do not copy the password to the clipboard (default is to copy password).

- **`generate-kubeconfig <cluster-name>`**: Generate a kubeconfig file for a cluster in the current directory.
  - Creates a file named `<cluster-name>-kubeconfig` in the current working directory.
  - Automatically authenticates using the cluster's kubeadmin password.
  - Overwrites existing kubeconfig files if they exist.
  - Example usage:

    ```bash
    ./mtv-dev generate-kubeconfig qemtv-02
    export KUBECONFIG=./qemtv-02-kubeconfig
    kubectl get nodes
    # or
    oc get pods --kubeconfig=./qemtv-02-kubeconfig
    ```

- **`run-tests <cluster-name> [template] [pytest-args...]`**: Build and run the test execution command.
  - `--provider <type>`: Source provider type (e.g., vmware8, ovirt).
  - `--storage <type>`: Storage class type (e.g., ceph, nfs, csi).
  - `--remote`: Flag for remote cluster tests.
  - `--data-collect`: Enable data collector for failed tests.
  - `--release-test`: Flag for release-specific tests.
  - `--pytest-args <args>`: Extra arguments to pass to pytest.
  - **Note:** To pass arguments directly to pytest, use `--` to separate Go flags from pytest flags, e.g.:

    ```bash
    ./mtv-dev run-tests <cluster> <template> -- --collectonly
    ```

- **`mtv-resources <cluster-name>`**: List all mtv-api-tests related resources on the cluster.

- **`csi-nfs-df <cluster-name>`**: Check the disk usage on the NFS CSI driver.

- **`ceph-df <cluster-name>`**: Run `ceph df` on the ceph tools pod (automatically enables ceph tools if needed).
  - `--watch`: Watch ceph df output every 10 seconds.

- **`ceph-cleanup <cluster-name>`**: Attempt to run ceph cleanup commands (automatically enables ceph tools if needed).
  - `--execute`: Execute the cleanup commands instead of just printing them.

- **`get-iib <mtv-version>`**: Extract latest Forklift FBC (File-Based Catalog) builds from the kuflox cluster.
  - Retrieves both production and stage builds for the specified MTV version (e.g., `2.9`).
  - Shows build information including OCP version, IIB format, and creation timestamps.
  - `--force-login`: Force re-authentication to kuflox cluster even if already logged in.
  - Supports SSO authentication with valid kerberos tickets (run `kinit` for SSO).
  - Example usage:

    ```bash
    ./mtv-dev get-iib 2.9
    ./mtv-dev get-iib 2.9 --force-login
    ```

  - **Sample Output:**

    ```text
    === MTV 2.9 Forklift FBC Builds ===

    📦 PRODUCTION BUILDS:

      OpenShift 4.17:
        Full MTV version: 2.9
        IIB: forklift-fbc-prod-v417:on-pr-abc123def456
        OCP version: 4.17
        Created: 2024-01-15 10:30:45 EST

      OpenShift 4.18:
        Full MTV version: 2.9
        IIB: forklift-fbc-prod-v418:on-pr-def456ghi789
        OCP version: 4.18
        Created: 2024-01-15 11:45:22 EST

    📦 STAGE BUILDS:

      OpenShift 4.17:
        Full MTV version: 2.9
        IIB: forklift-fbc-stage-v417:on-pr-ghi789abc123
        OCP version: 4.17
        Created: 2024-01-15 09:15:30 EST

    Summary: Found 2 production and 1 stage builds
    ```

- **`get-vms <provider-name>`**: List virtual machines from configured providers with dynamic configuration loading.
  - Loads provider configurations dynamically from `tests/tests_config/config.py` (the Python source of truth).
  - **Works from anywhere**: Automatically finds the config file by searching up from the executable location to the project root.
  - **Environment variable support**: Set `MTV_CONFIG_PATH` to specify a custom config file location.
  - Automatically excludes OVA and OpenShift providers.
  - By default shows all VMs; use `--running` to show only running/powered-on VMs.
  - **Available Providers (loaded dynamically):**
    - `vsphere-6.5`: VMware vSphere 6.5 environment
    - `vsphere-7.0.3`: VMware vSphere 7.0.3 environment
    - `vsphere-8.0.1`: VMware vSphere 8.0.1 environment
    - `ovirt-4.4.9`: oVirt/RHV 4.4.9 environment
    - `openstack-psi`: OpenStack PSI environment
  - **Optional Flags:**
    - `--output <format>`: Output format: table (default), json, or simple
    - `--running`: Show only running VMs (default: show all VMs)
    - `--list-providers`: List all available provider names and exit
  - **Example usage:**

    ```bash
    # Works from any directory - the tool finds the config file automatically
    mtv-dev get-vms vsphere-7.0.3

    # From project root
    ./tools/dev/mtv-dev get-vms vsphere-7.0.3

    # Using custom config file location
    MTV_CONFIG_PATH=/path/to/config.py mtv-dev get-vms vsphere-7.0.3

    # List only running VMs from oVirt environment
    mtv-dev get-vms ovirt-4.4.9 --running

    # List all VMs from OpenStack with JSON output
    mtv-dev get-vms openstack-psi --output json

    # Simple VM name list for scripting
    mtv-dev get-vms vsphere-8.0.1 --output simple

    # List all available provider names
    mtv-dev get-vms --list-providers
    ```

  - **Sample Table Output:**

    ```text
    Connecting to vsphere-7.0.3 provider (vmware)...
    Retrieving VM list...

    NAME                      PROVIDER     POWER STATE     CPU        MEMORY     GUEST OS  
    ------------------------------------------------------------------------------------------------
    web-server-01             vmware       poweredOn       4          8.0GB      RHEL 8.5  
    database-server           vmware       poweredOff      8          16.0GB     Ubuntu 22.04  
    test-vm-windows           vmware       poweredOn       2          4.0GB      Windows Server

    Summary:
    - Provider: vsphere-7.0.3 (vmware)
    - Total VMs: 3 (use --running to show only running VMs)
    ```

  - **Sample JSON Output:**

    ```json
    [
      {
        "name": "web-server-01",
        "provider": "vmware",
        "power_state": "poweredOn",
        "uuid": "421d2589-2a88-4aa8-b767-c5c84c4e4d2f",
        "cpu": 4,
        "memory_mb": 8192,
        "guest_os": "Red Hat Enterprise Linux 8 (64-bit)",
        "storage_gb": 100.50
      }
    ]
    ```

- **`completion [bash|zsh|fish|powershell]`**: Generate the autocompletion script for the specified shell.

---

## Legacy Scripts

The legacy scripts (`dev-tools.sh`, `build_run_tests_command.py`, and `check_nfs_csi_space.sh`) have been moved to
the `legacy/` folder for reference. This Go implementation (`mtv-dev`) provides complete feature parity with the
legacy scripts but is recommended for better performance, reliability, and user experience.

If you need to use the legacy scripts for any reason, they can be found in:

- `legacy/dev-tools.sh`
- `legacy/build_run_tests_command.py`
