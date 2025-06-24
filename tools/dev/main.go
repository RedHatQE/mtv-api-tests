package main

import (
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
	listClustersCmd.Flags().Bool("timing", false, "Show timing information for each cluster")
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

	rootCmd.AddCommand(&cobra.Command{
		Use:               "mtv-resources <cluster-name>",
		Short:             "List all mtv-api-tests related resources on the cluster.",
		Args:              cobra.ExactArgs(1),
		Run:               mtvResources,
		ValidArgsFunction: getClusterNames,
	})

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
	rootCmd.AddCommand(getIIBCmd)
}
