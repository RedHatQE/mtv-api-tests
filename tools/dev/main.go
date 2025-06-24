package main

import (
	"context"
	"fmt"
	"io/fs"
	"log"
	"os"

	"mtv-dev/tui"

	"github.com/spf13/cobra"
)

// Bridge implementation to connect main package functions to TUI
type mainClusterLoaderDeps struct{}

func (d *mainClusterLoaderDeps) ReadDir(path string) ([]fs.DirEntry, error) {
	return readDir(path)
}

func (d *mainClusterLoaderDeps) EnsureLoggedInSilent(clusterName string) error {
	// Silent version that doesn't print to stdout
	_, err := buildOCPClient(clusterName)
	if err != nil {
		return fmt.Errorf("failed to connect to cluster %s: %w", clusterName, err)
	}
	return nil
}

func (d *mainClusterLoaderDeps) GetClusterInfoSilent(clusterName string) (*tui.ClusterInfo, error) {
	info, err := getClusterInfo(clusterName)
	if err != nil {
		return nil, err
	}

	// Convert from main.ClusterInfo to tui.ClusterInfo
	return &tui.ClusterInfo{
		Name:       info.Name,
		OCPVersion: info.OCPVersion,
		MTVVersion: info.MTVVersion,
		CNVVersion: info.CNVVersion,
		IIB:        info.IIB,
		ConsoleURL: info.ConsoleURL,
	}, nil
}

func (d *mainClusterLoaderDeps) GetClusterPassword(clusterName string) (string, error) {
	return getClusterPassword(clusterName)
}

// Bridge implementation for IIB data loading
type mainIIBLoaderDeps struct{}

func (d *mainIIBLoaderDeps) GetForkliftBuilds(environment string) ([]tui.IIBInfo, error) {
	builds, err := getForkliftBuilds(environment)
	if err != nil {
		return nil, err
	}

	// Convert from main.IIBInfo to tui.IIBInfo
	var tuiBuilds []tui.IIBInfo
	for _, build := range builds {
		tuiBuilds = append(tuiBuilds, tui.IIBInfo{
			OCPVersion:  build.OCPVersion,
			MTVVersion:  build.MTVVersion,
			IIB:         build.IIB,
			Snapshot:    build.Snapshot,
			Created:     build.Created,
			Image:       build.Image,
			Environment: build.Environment,
		})
	}

	return tuiBuilds, nil
}

func (d *mainIIBLoaderDeps) CheckKufloxLogin() bool {
	return checkKufloxLogin()
}

func (d *mainIIBLoaderDeps) LoginToKuflox() error {
	return loginToKuflox()
}

// Bridge implementation for provider data loading
type mainProviderLoaderDeps struct{}

func (d *mainProviderLoaderDeps) LoadProviderConfigs() (map[string]tui.ProviderConfig, error) {
	configs, err := loadProviderConfigs()
	if err != nil {
		return nil, err
	}

	// Convert from main.ProviderConfig to tui.ProviderConfig
	tuiConfigs := make(map[string]tui.ProviderConfig)
	for name, config := range configs {
		tuiConfigs[name] = tui.ProviderConfig{
			Type:        config.Type,
			URL:         config.URL,
			Username:    config.Username,
			Password:    config.Password,
			Insecure:    config.Insecure,
			ExtraParams: config.ExtraParams,
		}
	}

	return tuiConfigs, nil
}

func (d *mainProviderLoaderDeps) CreateProvider(providerType, url, username, password string, insecure bool, extraParams map[string]string) (tui.VMProvider, error) {
	provider, err := CreateProvider(providerType, url, username, password, insecure, extraParams)
	if err != nil {
		return nil, err
	}
	return &vmProviderWrapper{provider: provider}, nil
}

// Wrapper to adapt main VMProvider to tui.VMProvider interface
type vmProviderWrapper struct {
	provider VMProvider
}

func (w *vmProviderWrapper) Connect() error {
	return w.provider.Connect()
}

func (w *vmProviderWrapper) ListVMs(ctx context.Context) ([]tui.VMInfo, error) {
	vms, err := w.provider.ListVMs(ctx)
	if err != nil {
		return nil, err
	}

	// Convert from main.VMInfo to tui.VMInfo
	var tuiVMs []tui.VMInfo
	for _, vm := range vms {
		tuiVMs = append(tuiVMs, tui.VMInfo{
			Name:         vm.Name,
			Provider:     vm.Provider,
			PowerState:   vm.PowerState,
			UUID:         vm.UUID,
			CPU:          vm.CPU,
			MemoryMB:     vm.MemoryMB,
			GuestOS:      vm.GuestOS,
			IPAddresses:  vm.IPAddresses,
			StorageGB:    vm.StorageGB,
			CreationTime: vm.CreationTime,
			LastModified: vm.LastModified,
			Tags:         vm.Tags,
			Cluster:      vm.Cluster,
			Host:         vm.Host,
			ResourcePool: vm.ResourcePool,
			Networks:     vm.Networks,
		})
	}

	return tuiVMs, nil
}

func (w *vmProviderWrapper) GetVM(ctx context.Context, name string) (*tui.VMInfo, error) {
	vm, err := w.provider.GetVM(ctx, name)
	if err != nil {
		return nil, err
	}

	// Convert from main.VMInfo to tui.VMInfo
	return &tui.VMInfo{
		Name:         vm.Name,
		Provider:     vm.Provider,
		PowerState:   vm.PowerState,
		UUID:         vm.UUID,
		CPU:          vm.CPU,
		MemoryMB:     vm.MemoryMB,
		GuestOS:      vm.GuestOS,
		IPAddresses:  vm.IPAddresses,
		StorageGB:    vm.StorageGB,
		CreationTime: vm.CreationTime,
		LastModified: vm.LastModified,
		Tags:         vm.Tags,
		Cluster:      vm.Cluster,
		Host:         vm.Host,
		ResourcePool: vm.ResourcePool,
		Networks:     vm.Networks,
	}, nil
}

func (w *vmProviderWrapper) Close() error {
	return w.provider.Close()
}

func (w *vmProviderWrapper) GetProviderType() string {
	return w.provider.GetProviderType()
}

func main() {
	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}

func init() {
	cobra.OnInitialize(func() {
		if err := ensureNfsMounted(); err != nil {
			log.Fatal(err)
		}
	})

	// List clusters command (fast concurrent implementation)
	listClustersCmd := &cobra.Command{
		Use:   "list-clusters",
		Short: "List all available clusters.",
		Run:   listClusters,
	}
	listClustersCmd.Flags().BoolVar(&full, "full", false, "Show full details for each cluster")
	listClustersCmd.Flags().Bool("verbose", false, "Show detailed error information for failed clusters")
	listClustersCmd.Flags().String("output", "table", "Output format: table, json, or simple")

	// Register flag completions
	_ = listClustersCmd.RegisterFlagCompletionFunc("output", func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		return []string{"table", "json", "simple"}, cobra.ShellCompDirectiveNoFileComp
	})

	rootCmd.AddCommand(listClustersCmd)

	clusterPasswordCmd := &cobra.Command{
		Use:               "cluster-password <cluster-name>",
		Short:             "Get the kubeadmin password for a cluster.",
		Args:              cobra.ExactArgs(1),
		Run:               clusterPassword,
		ValidArgsFunction: getClusterNames,
	}
	clusterPasswordCmd.Flags().Bool("no-copy", false, "Do not copy the password to the clipboard")
	rootCmd.AddCommand(clusterPasswordCmd)

	clusterLoginCmd := &cobra.Command{
		Use:               "cluster-login <cluster-name>",
		Short:             "Display login command and cluster info.",
		Args:              cobra.ExactArgs(1),
		Run:               clusterLogin,
		ValidArgsFunction: getClusterNames,
	}
	clusterLoginCmd.Flags().Bool("no-copy", false, "Do not copy the login command to the clipboard")
	clusterLoginCmd.Flags().String("output", "table", "Output format: table or json")

	// Register flag completions
	_ = clusterLoginCmd.RegisterFlagCompletionFunc("output", func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		return []string{"table", "json"}, cobra.ShellCompDirectiveNoFileComp
	})

	rootCmd.AddCommand(clusterLoginCmd)

	generateKubeconfigCmd := &cobra.Command{
		Use:               "generate-kubeconfig <cluster-name>",
		Short:             "Generate a kubeconfig file for a cluster in the current directory.",
		Long:              "Generate a kubeconfig file for the specified cluster and save it in the current directory with the format '<cluster-name>-kubeconfig'.",
		Args:              cobra.ExactArgs(1),
		Run:               generateKubeconfig,
		ValidArgsFunction: getClusterNames,
	}
	rootCmd.AddCommand(generateKubeconfigCmd)

	runTestsCmd := &cobra.Command{
		Use:   "run-tests <cluster-name> [test-args...]",
		Short: "Build and run the test execution command.",
		Args:  cobra.ArbitraryArgs,
		Run:   runTests,
		ValidArgsFunction: func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
			if len(args) == 0 {
				// First argument: cluster name
				return getClusterNames(cmd, args, toComplete)
			} else if len(args) == 1 {
				// Second argument: template name
				return getTemplateNames(cmd, args, toComplete)
			}
			// No more completions for additional arguments
			return nil, cobra.ShellCompDirectiveNoFileComp
		},
	}
	runTestsCmd.Flags().String("provider", "", "Source provider type (e.g., vmware8, ovirt).")
	runTestsCmd.Flags().String("storage", "", "Storage class type (e.g., ceph, nfs, csi).")
	runTestsCmd.Flags().Bool("remote", false, "Flag for remote cluster tests.")
	runTestsCmd.Flags().Bool("data-collect", false, "Enable data collector for failed tests.")
	runTestsCmd.Flags().Bool("release-test", false, "Flag for release-specific tests.")
	runTestsCmd.Flags().String("pytest-args", "", "Extra arguments to pass to pytest.")

	// Register flag completions
	_ = runTestsCmd.RegisterFlagCompletionFunc("provider", getProviderNames)
	_ = runTestsCmd.RegisterFlagCompletionFunc("storage", getStorageNames)

	rootCmd.AddCommand(runTestsCmd)

	mtvResourcesCmd := &cobra.Command{
		Use:               "mtv-resources <cluster-name>",
		Short:             "List all mtv-api-tests related resources on the cluster.",
		Args:              cobra.ExactArgs(1),
		Run:               mtvResources,
		ValidArgsFunction: getClusterNames,
	}
	mtvResourcesCmd.Flags().String("output", "table", "Output format: table or json")

	// Register flag completions
	_ = mtvResourcesCmd.RegisterFlagCompletionFunc("output", func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		return []string{"table", "json"}, cobra.ShellCompDirectiveNoFileComp
	})

	rootCmd.AddCommand(mtvResourcesCmd)

	rootCmd.AddCommand(&cobra.Command{
		Use:               "csi-nfs-df <cluster-name>",
		Short:             "Check the disk usage on the NFS CSI driver.",
		Args:              cobra.ExactArgs(1),
		Run:               csiNfsDf,
		ValidArgsFunction: getClusterNames,
	})

	cephDfCmd := &cobra.Command{
		Use:               "ceph-df <cluster-name>",
		Short:             "Run 'ceph df' on the ceph tools pod.",
		Args:              cobra.ExactArgs(1),
		Run:               cephDf,
		ValidArgsFunction: getClusterNames,
	}
	cephDfCmd.Flags().Bool("watch", false, "Watch ceph df output every 10 seconds.")
	rootCmd.AddCommand(cephDfCmd)

	cephCleanupCmd := &cobra.Command{
		Use:               "ceph-cleanup <cluster-name>",
		Short:             "Attempt to run ceph cleanup commands.",
		Args:              cobra.ExactArgs(1),
		Run:               cephCleanup,
		ValidArgsFunction: getClusterNames,
	}
	cephCleanupCmd.Flags().Bool("execute", false, "Execute the cleanup commands instead of just printing them")
	rootCmd.AddCommand(cephCleanupCmd)

	// TUI command with dependency injection
	tuiCmd := &cobra.Command{
		Use:   "tui",
		Short: "Launch the Terminal User Interface (TUI) for interactive mode.",
		Long: `Launch the Terminal User Interface (TUI) for interactive mode.
This provides a user-friendly menu-driven interface to browse clusters,
configure tests, and perform operations without memorizing command syntax.`,
		Run: func(cmd *cobra.Command, args []string) {
			// Inject real dependencies into TUI
			tui.SetClusterLoaderDeps(&mainClusterLoaderDeps{})
			tui.SetIIBLoaderDeps(&mainIIBLoaderDeps{})
			tui.SetProviderLoaderDeps(&mainProviderLoaderDeps{})
			tui.RunTUI()
		},
	}
	rootCmd.AddCommand(tuiCmd)

	// Get IIB command
	getIIBCmd := &cobra.Command{
		Use:   "get-iib <mtv-version>",
		Short: "Get the latest Forklift FBC builds from kuflox cluster for a specific MTV version.",
		Long: `Get the latest Forklift FBC (File-Based Catalog) builds from the kuflox cluster
for a specific MTV version. Returns both production and stage builds for
OpenShift versions 4.17, 4.18, and 4.19.

The mtv-version should be in major.minor format (e.g., '2.9').

Example:
  mtv-dev get-iib 2.9

This will show:
- Full MTV version
- IIB (Index Image Bundle) reference
- OpenShift version
- Build timestamps and details`,
		Args: cobra.ExactArgs(1),
		Run:  getIIB,
	}
	getIIBCmd.Flags().Bool("force-login", false, "Force re-authentication even if already logged in")
	getIIBCmd.Flags().String("output", "table", "Output format: table or json")

	// Register flag completions
	_ = getIIBCmd.RegisterFlagCompletionFunc("output", func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		return []string{"table", "json"}, cobra.ShellCompDirectiveNoFileComp
	})

	rootCmd.AddCommand(getIIBCmd)

	// Get VMs command
	getVMsCmd := &cobra.Command{
		Use:   "get-vms <provider-name>",
		Short: "List virtual machines from configured providers.",
		Long: `List virtual machines from pre-configured virtualization providers.
Uses provider configurations loaded from the Python config file.

Provider configurations are loaded dynamically from tests/tests_config/config.py
which excludes OVA and OpenShift providers.

By default, all VMs are shown. Use --running to show only running VMs.

Examples:
  # List all VMs from vSphere 7.0.3 environment
  mtv-dev get-vms vsphere-7.0.3

  # List only running VMs from oVirt environment
  mtv-dev get-vms ovirt-4.4.9 --running

  # List all VMs from OpenStack with JSON output
  mtv-dev get-vms openstack-psi --output json

  # Simple list of VM names for scripting
  mtv-dev get-vms vsphere-8.0.1 --output simple

  # List all available provider names
  mtv-dev get-vms --list-providers`,
		Args: cobra.RangeArgs(0, 1),
		Run:  getVMs,
		ValidArgsFunction: func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
			if len(args) == 0 {
				// Load provider configurations for tab completion
				if vmProviderConfigs, err := loadProviderConfigs(); err == nil {
					var providers []string
					for name := range vmProviderConfigs {
						providers = append(providers, name)
					}
					return providers, cobra.ShellCompDirectiveNoFileComp
				}
				// If loading fails, return empty list - config.py is the source of truth
				return []string{}, cobra.ShellCompDirectiveNoFileComp
			}
			// No more completions for additional arguments
			return nil, cobra.ShellCompDirectiveNoFileComp
		},
	}
	getVMsCmd.Flags().String("output", "table", "Output format: table, json, or simple")
	getVMsCmd.Flags().Bool("running", false, "Show only running VMs (default: show all VMs)")
	getVMsCmd.Flags().Bool("list-providers", false, "List all available provider names and exit")

	// Register flag completions
	_ = getVMsCmd.RegisterFlagCompletionFunc("output", func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		return []string{"table", "json", "simple"}, cobra.ShellCompDirectiveNoFileComp
	})

	rootCmd.AddCommand(getVMsCmd)
}
