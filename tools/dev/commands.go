package main

import (
	"bufio"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
)

// Fast concurrent list-clusters implementation
func listClusters(cmd *cobra.Command, args []string) {
	clusters, err := readDir(CLUSTERS_PATH)
	if err != nil {
		log.Fatalf("%sFailed to read clusters directory: %v%s", ColorRed, err, ColorReset)
	}
	if len(clusters) == 0 {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sNo clusters found.%s\n", ColorYellow, ColorReset)
		return
	}

	verbose, _ := cmd.Flags().GetBool("verbose")
	showTiming, _ := cmd.Flags().GetBool("timing")
	start := time.Now()

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sChecking cluster accessibility...%s\n", ColorCyan, ColorReset)

	// Filter cluster names
	var clusterNames []string
	for _, entry := range clusters {
		if !entry.IsDir() {
			continue
		}
		name := entry.Name()
		if strings.HasPrefix(name, "qemtv-") || strings.HasPrefix(name, "qemtvd-") {
			clusterNames = append(clusterNames, name)
		}
	}

	if len(clusterNames) == 0 {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sNo matching clusters found.%s\n", ColorYellow, ColorReset)
		return
	}

	// Simple concurrent processing (the real performance win)
	type clusterResult struct {
		info ClusterInfo
		err  error
	}
	resultChan := make(chan clusterResult, len(clusterNames))
	var liveClusterInfos []ClusterInfo

	// Launch one goroutine per cluster (no complex worker pools)
	for _, clusterName := range clusterNames {
		go func(name string) {
			defer func() {
				if r := recover(); r != nil {
					resultChan <- clusterResult{err: fmt.Errorf("panic in %s: %v", name, r)}
				}
			}()

			if err := ensureLoggedIn(name); err != nil {
				resultChan <- clusterResult{err: fmt.Errorf("login failed for %s: %w", name, err)}
				return
			}
			info, err := getClusterInfo(name)
			if err != nil {
				resultChan <- clusterResult{err: fmt.Errorf("cluster info failed for %s: %w", name, err)}
				return
			}
			resultChan <- clusterResult{info: *info}
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s%s is accessible%s\n", ColorGreen, name, ColorReset)
		}(clusterName)
	}

	// Collect results with reasonable timeout
	collected := 0
	errorCount := 0
	timeout := time.After(75 * time.Second)
	for collected < len(clusterNames) {
		select {
		case result := <-resultChan:
			if result.err == nil {
				liveClusterInfos = append(liveClusterInfos, result.info)
			} else {
				errorCount++
				if verbose {
					_, _ = fmt.Fprintf(cmd.OutOrStderr(), "Warning: %v\n", result.err)
				}
			}
			collected++
		case <-timeout:
			_, _ = fmt.Fprintf(cmd.OutOrStderr(), "Timeout reached after 75 seconds, processed %d/%d clusters...\n", collected, len(clusterNames))
			goto done
		}
	}

done:
	if len(liveClusterInfos) == 0 {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sNo live clusters found.%s\n", ColorYellow, ColorReset)
		return
	}

	// Sort clusters by name for consistent output
	sort.Slice(liveClusterInfos, func(i, j int) bool {
		return liveClusterInfos[i].Name < liveClusterInfos[j].Name
	})

	if !full {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%sAvailable live clusters:%s\n", ColorCyan, ColorReset)
		for _, info := range liveClusterInfos {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s- %s%s\n", ColorGreen, info.Name, ColorReset)
		}
	} else {
		// Full table output
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s%-15s %-12s %-15s %-15s %s%s\n",
			ColorCyan, "CLUSTER", "OCP", "MTV", "CNV", "IIB", ColorReset)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s%s%s\n", ColorCyan, strings.Repeat("-", 80), ColorReset)

		for _, info := range liveClusterInfos {
			// Handle missing data with proper fallbacks
			ocpVersion := info.OCPVersion
			if ocpVersion == "" {
				ocpVersion = "Unknown"
			}

			mtvVersion := info.MTVVersion
			if mtvVersion == "" {
				mtvVersion = "Unknown"
			}

			cnvVersion := info.CNVVersion
			if cnvVersion == "" {
				cnvVersion = "Unknown"
			}

			iibVersion := info.IIB
			if iibVersion == "" {
				iibVersion = "N/A"
			}
			// Truncate very long IIB names for better table formatting
			if len(iibVersion) > 35 {
				iibVersion = iibVersion[:32] + "..."
			}

			// Color code the status for better visibility
			var mtvDisplay, cnvDisplay string
			if mtvVersion == "Not installed" || mtvVersion == "Unknown" {
				mtvDisplay = fmt.Sprintf("%s%s%s", ColorYellow, mtvVersion, ColorReset)
			} else {
				mtvDisplay = fmt.Sprintf("%s%s%s", ColorGreen, mtvVersion, ColorReset)
			}

			if cnvVersion == "Not installed" || cnvVersion == "Unknown" {
				cnvDisplay = fmt.Sprintf("%s%s%s", ColorYellow, cnvVersion, ColorReset)
			} else {
				cnvDisplay = fmt.Sprintf("%s%s%s", ColorGreen, cnvVersion, ColorReset)
			}

			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%-15s %-12s %-24s %-24s %s\n",
				info.Name, ocpVersion, mtvDisplay, cnvDisplay, iibVersion)
		}
	}

	// Summary
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%sSummary:%s\n", ColorCyan, ColorReset)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "- Total clusters: %d\n", len(liveClusterInfos))
	if errorCount > 0 {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "- Failed clusters: %d\n", errorCount)
	}
	if showTiming {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "- Total time: %.2fs\n", time.Since(start).Seconds())
	}
}

func clusterPassword(cmd *cobra.Command, args []string) {
	clusterName := args[0]
	password, err := getClusterPassword(clusterName)
	if err != nil {
		log.Fatalf("Could not get password for cluster %s: %v", clusterName, err)
	}
	noCopy, _ := cmd.Flags().GetBool("no-copy")
	_, _ = fmt.Fprintln(cmd.OutOrStdout(), password)
	if !noCopy {
		if err := clipboardWriteAll(password); err != nil {
			_, _ = fmt.Fprintln(cmd.OutOrStderr(), "Warning: could not copy password to clipboard.", err)
		} else {
			_, _ = fmt.Fprintln(cmd.OutOrStderr(), "Password copied to clipboard.")
		}
	}
}

func clusterLogin(cmd *cobra.Command, args []string) {
	clusterName := args[0]
	noCopy, _ := cmd.Flags().GetBool("no-copy")

	if err := ensureLoggedIn(clusterName); err != nil {
		log.Fatal(err)
	}

	password, err := getClusterPassword(clusterName)
	if err != nil {
		log.Fatalf("Could not get password for cluster %s: %v", clusterName, err)
	}

	apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", clusterName)
	loginCmdStr := fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, password)

	if !noCopy {
		if err := clipboardWriteAll(password); err != nil {
			_, _ = fmt.Fprintln(cmd.OutOrStderr(), "Warning: could not copy password to clipboard.", err)
		} else {
			_, _ = fmt.Fprintln(cmd.OutOrStderr(), "Password copied to clipboard.")
		}
	}

	info, err := getClusterInfo(clusterName)
	if err != nil {
		log.Fatalf("Could not get cluster info: %v", err)
	}

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "OpenShift Cluster Info -- [%s]\n", clusterName)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── Username: %s\n", "kubeadmin")
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── Password: %s\n", password)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── Login: %s\n", loginCmdStr)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── Console: %s\n", info.ConsoleURL)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── OCP version: %s\n", info.OCPVersion)
	if info.MTVVersion != "Not installed" {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── MTV version: %s (%s)\n", info.MTVVersion, info.IIB)
	} else {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "├── MTV version: %s\n", info.MTVVersion)
	}
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "└── CNV version: %s\n", info.CNVVersion)
}

func runTests(cmd *cobra.Command, args []string) {
	if len(args) < 1 {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sYou must specify at least a cluster name.%s\n", ColorRed, ColorReset)
		return
	}
	clusterName := args[0]
	pytestExtraArgs := args[1:]

	// Always perform oc login in the shell before running tests
	password, err := getClusterPassword(clusterName)
	if err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sCould not get password for cluster %s: %v%s\n", ColorRed, clusterName, err, ColorReset)
		return
	}

	apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", clusterName)
	loginCmdStr := fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, password)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sLogging in to cluster before running tests...%s\n", ColorYellow, ColorReset)
	loginCmd := execCommand("bash", "-c", loginCmdStr)
	if err := loginCmd.Run(); err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sFailed to log in to cluster: %v%s\n", ColorRed, err, ColorReset)
		return
	}
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sLogin successful. Running tests...%s\n", ColorGreen, ColorReset)

	// Get flags
	provider, _ := cmd.Flags().GetString("provider")
	storage, _ := cmd.Flags().GetString("storage")
	isRemote, _ := cmd.Flags().GetBool("remote")
	dataCollect, _ := cmd.Flags().GetBool("data-collect")
	releaseTest, _ := cmd.Flags().GetBool("release-test")

	var providerKey string
	var storageKey string

	// Handle pre-defined templates or manual flags
	if len(pytestExtraArgs) > 0 {
		template, exists := runsTemplates[pytestExtraArgs[0]]
		if exists {
			providerKey = template.Provider
			storageKey = template.Storage
			isRemote = template.Remote
			pytestExtraArgs = pytestExtraArgs[1:]
		}
	}

	if provider != "" {
		providerKey = provider
	}
	if storage != "" {
		storageKey = storage
	}

	if providerKey == "" || storageKey == "" {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sError: You must specify a pre-defined template or both --provider and --storage.%s\n", ColorRed, ColorReset)
		return
	}

	providerConfig, ok := providerMap[providerKey]
	if !ok {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sError: Invalid provider '%s'%s\n", ColorRed, providerKey, ColorReset)
		return
	}
	storageClass, ok := storageMap[storageKey]
	if !ok {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sError: Invalid storage '%s'%s\n", ColorRed, storageKey, ColorReset)
		return
	}

	clusterVersion, err := getClusterVersion(clusterName)
	if err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sFailed to get cluster version: %v%s\n", ColorRed, err, ColorReset)
		return
	}

	mountPath := os.Getenv("MOUNT_PATH")
	if mountPath == "" {
		// Set default mount path to the clusters directory
		mountPath = CLUSTERS_PATH
		if err := os.Setenv("MOUNT_PATH", mountPath); err != nil {
			_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sWarning: could not set MOUNT_PATH environment variable: %v%s\n", ColorRed, err, ColorReset)
		}
	}
	user := os.Getenv("USER")
	if user == "" {
		user = "unknown"
	}

	baseCmdParts := []string{
		"uv", "run", "pytest", "-s",
		fmt.Sprintf("--tc=target_ocp_version:%s", clusterVersion),
		"--tc=insecure_verify_skip:true",
		fmt.Sprintf("--tc=mount_root:%s", mountPath),
		fmt.Sprintf("--tc=source_provider_type:%s", providerConfig.Type),
		fmt.Sprintf("--tc=source_provider_version:%s", providerConfig.Version),
		fmt.Sprintf("--tc=target_namespace:mtv-api-tests-%s-%s", providerKey, user),
		fmt.Sprintf("--tc=storage_class:%s", storageClass),
	}

	if isRemote {
		clusterNameEnv := os.Getenv("CLUSTER_NAME")
		if clusterNameEnv == "" {
			clusterNameEnv = clusterName
		}
		baseCmdParts = append(baseCmdParts, "-m", "remote", fmt.Sprintf("--tc=remote_ocp_cluster:%s", clusterNameEnv))
	}

	if !dataCollect {
		baseCmdParts = append(baseCmdParts, "--skip-data-collector")
	}

	if !releaseTest {
		baseCmdParts = append(baseCmdParts, "--tc=matrix_test:true", "-m", "tier0")
	}

	if len(pytestExtraArgs) > 0 {
		baseCmdParts = append(baseCmdParts, pytestExtraArgs...)
	}

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "Running command:\n%s\n", strings.Join(baseCmdParts, " "))
	if err := os.Setenv("OPENSHIFT_PYTHON_WRAPPER_LOG_LEVEL", "DEBUG"); err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "Warning: could not set log level: %v\n", err)
	}

	// Execute the command with colors preserved
	testCmd := execCommand(baseCmdParts[0], baseCmdParts[1:]...)

	// Set the command's stdout and stderr to preserve colors and interactive output
	if realCmd, ok := testCmd.(*exec.Cmd); ok {
		realCmd.Stdout = os.Stdout
		realCmd.Stderr = os.Stderr
		realCmd.Stdin = os.Stdin
	}

	if err := testCmd.Run(); err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "Test command failed: %v\n", err)
		return
	}
}

// Refactored mtvResources to accept dependencies
func mtvResourcesWithDeps(cmd *cobra.Command, args []string, ensureLoggedInFunc func(string) error, execCommandFunc func(string, ...string) CmdRunner) {
	clusterName := args[0]
	if err := ensureLoggedInFunc(clusterName); err != nil {
		_, _ = fmt.Fprintln(cmd.OutOrStderr(), "Failed to initialize OCP client:", err)
		return
	}
	resources := []string{"ns", "pods", "dv", "pvc", "pv", "plan", "migration", "storagemap", "networkmap", "provider", "host", "secret", "net-attach-def", "hook", "vm", "vmi"}
	for _, resource := range resources {
		ocCmd := execCommandFunc("oc", "get", resource, "-A")
		output, err := ocCmd.CombinedOutput()
		if err != nil {
			continue // skip resources that don't exist
		}
		lines := strings.Split(string(output), "\n")
		var found bool
		var filtered []string
		for _, line := range lines {
			if strings.Contains(line, "mtv-api") {
				filtered = append(filtered, line)
				found = true
			}
		}
		if found {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s:\n", resource)
			for _, line := range filtered {
				_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    %s\n", line)
			}
			_, _ = fmt.Fprintln(cmd.OutOrStdout())
		}
	}
}

// Wrapper for Cobra to use real dependencies
func mtvResources(cmd *cobra.Command, args []string) {
	mtvResourcesWithDeps(cmd, args, ensureLoggedIn, execCommand)
}

func csiNfsDf(cmd *cobra.Command, args []string) {
	clusterName := args[0]
	if err := ensureLoggedIn(clusterName); err != nil {
		log.Fatalf("%sFailed to initialize OCP client: %v%s", ColorRed, err, ColorReset)
	}

	// Initialize the global ocpClient
	var err error
	ocpClient, err = buildOCPClient(clusterName)
	if err != nil {
		log.Fatalf("%sFailed to build OCP client: %v%s", ColorRed, err, ColorReset)
	}

	fmt.Println("Finding nfs-csi storage class...")
	storageClassName := "nfs-csi"

	// 1. Get nfs-server from storage class
	sc, err := ocpClient.KubeClient.StorageV1().StorageClasses().Get(context.TODO(), storageClassName, metav1.GetOptions{})
	if err != nil {
		log.Fatalf("%sError: Could not find the '%s' storage class or the NFS server parameter.%s", ColorRed, storageClassName, ColorReset)
	}
	nfsServer := sc.Parameters["server"]
	if nfsServer == "" {
		log.Fatalf("%sError: Could not find the NFS server parameter in storage class.%s", ColorRed, ColorReset)
	}
	fmt.Printf("%sFound NFS server: %s%s\n", ColorGreen, nfsServer, ColorReset)

	fmt.Println("Searching for an existing pod using a bound nfs-csi volume...")

	var dfOutput string
	var foundExistingPod bool

	// 2. Try to find existing pod using nfs-csi PVC
	pvcs, err := ocpClient.KubeClient.CoreV1().PersistentVolumeClaims("").List(context.TODO(), metav1.ListOptions{})
	if err == nil {
		for _, pvc := range pvcs.Items {
			if pvc.Spec.StorageClassName != nil && *pvc.Spec.StorageClassName == storageClassName && pvc.Status.Phase == v1.ClaimBound {
				fmt.Printf("Found existing PVC '%s' in namespace '%s'. Looking for a pod using it.\n", pvc.Name, pvc.Namespace)

				// Find running pod using this PVC
				pods, err := ocpClient.KubeClient.CoreV1().Pods(pvc.Namespace).List(context.TODO(), metav1.ListOptions{})
				if err != nil {
					continue
				}

				for _, pod := range pods.Items {
					if pod.Status.Phase == v1.PodRunning {
						for _, volume := range pod.Spec.Volumes {
							if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ClaimName == pvc.Name {
								fmt.Printf("%sFound existing pod '%s' using the PVC.%s\n", ColorGreen, pod.Name, ColorReset)
								fmt.Printf("Executing 'df -h' in existing pod '%s'...\n", pod.Name)
								stdout, stderr, err := executeInPod(ocpClient, pod.Namespace, pod.Name, "", []string{"df", "-h"})
								if err != nil {
									log.Printf("%sWarning: failed to run 'df -h' in pod %s: %v. Stderr: %s%s", ColorYellow, pod.Name, err, stderr, ColorReset)
									continue
								}
								dfOutput = stdout
								foundExistingPod = true
								break
							}
						}
					}
					if foundExistingPod {
						break
					}
				}
				if foundExistingPod {
					break
				}
			}
		}
	}

	if !foundExistingPod {
		fmt.Println("No running pod found using an existing nfs-csi PVC. Creating temporary resources...")
		dfOutput = createTempResourcesAndGetDf(ocpClient)
	}

	// 3. Parse and display results
	if dfOutput == "" {
		log.Fatalf("%sError: Failed to get 'df -h' output from any pod.%s", ColorRed, ColorReset)
	}

	var nfsUsageLine string
	for _, line := range strings.Split(dfOutput, "\n") {
		if strings.Contains(line, nfsServer) {
			nfsUsageLine = line
			break
		}
	}

	if nfsUsageLine == "" {
		log.Fatalf("%sError: Could not find the NFS mount from server '%s' in the 'df -h' output.\nFull 'df -h' output from the pod:\n%s%s", ColorRed, nfsServer, dfOutput, ColorReset)
	}

	fmt.Printf("%sSuccess! Found storage information.%s\n", ColorGreen, ColorReset)
	fmt.Println("")
	fmt.Printf("%s--- NFS-CSI Storage Usage ---%s\n", ColorCyan, ColorReset)

	fields := strings.Fields(nfsUsageLine)
	if len(fields) >= 6 {
		fmt.Printf("Filesystem: %s\n", fields[0])
		fmt.Printf("Total Size: %s\n", fields[1])
		fmt.Printf("Used Space: %s\n", fields[2])
		fmt.Printf("Available Space: %s\n", fields[3])
		fmt.Printf("Usage: %s\n", fields[4])
		fmt.Printf("Mount Point: %s\n", fields[5])
	} else {
		fmt.Printf("Raw output: %s\n", nfsUsageLine)
	}
	fmt.Println("-----------------------------")
}

func cephDf(cmd *cobra.Command, args []string) {
	clusterName := args[0]
	watch, _ := cmd.Flags().GetBool("watch")

	toolsPodName, err := enableCephTools(clusterName)
	if err != nil {
		log.Fatalf("%sCould not enable ceph tools: %v%s", ColorRed, err, ColorReset)
	}

	// Initialize the global ocpClient for executeInPod
	ocpClient, err = buildOCPClient(clusterName)
	if err != nil {
		log.Fatalf("%sFailed to build OCP client: %v%s", ColorRed, err, ColorReset)
	}

	for {
		stdout, stderr, err := executeInPod(ocpClient, "openshift-storage", toolsPodName, "", []string{"ceph", "df"})
		if err != nil {
			log.Fatalf("%sFailed to execute 'ceph df': %v\nSTDOUT: %s\nSTDERR: %s%s", ColorRed, err, stdout, stderr, ColorReset)
		}
		fmt.Println(stdout)
		if !watch {
			break
		}
		time.Sleep(10 * time.Second)
	}
}

func cephCleanup(cmd *cobra.Command, args []string) {
	clusterName := args[0]
	execute, _ := cmd.Flags().GetBool("execute")

	toolsPodName, err := enableCephTools(clusterName)
	if err != nil {
		log.Fatalf("%sCould not enable ceph tools: %v%s", ColorRed, err, ColorReset)
	}

	// Initialize the global ocpClient for executeInPod
	ocpClient, err = buildOCPClient(clusterName)
	if err != nil {
		log.Fatalf("%sFailed to build OCP client: %v%s", ColorRed, err, ColorReset)
	}

	cephPool := "ocs-storagecluster-cephblockpool"
	var commands []string

	// Set OSD full ratio to 0.90
	commands = append(commands, "ceph osd set-full-ratio 0.90")

	// Get list of RBD images
	fmt.Printf("Getting list of RBD images in pool %s...\n", cephPool)
	rbdListOutput, rbdStderr, rbdErr := executeInPod(ocpClient, "openshift-storage", toolsPodName, "", []string{"rbd", "ls", cephPool})
	if rbdErr != nil {
		log.Printf("Warning: Failed to list RBD images: %v\nSTDERR: %s", rbdErr, rbdStderr)
	} else {
		rbdImages := strings.Fields(strings.TrimSpace(rbdListOutput))
		for _, image := range rbdImages {
			if image != "" {
				imagePath := cephPool + "/" + image
				// Purge all snapshots for the image
				commands = append(commands, fmt.Sprintf("rbd snap purge %s", imagePath))
				// Remove the image itself
				commands = append(commands, fmt.Sprintf("rbd rm %s", imagePath))
			}
		}
	}

	// Get list of trash items
	fmt.Printf("Getting list of trash items in pool %s...\n", cephPool)
	trashListOutput, trashStderr, trashErr := executeInPod(ocpClient, "openshift-storage", toolsPodName, "", []string{"rbd", "trash", "list", cephPool})
	if trashErr != nil {
		log.Printf("Warning: Failed to list trash items: %v\nSTDERR: %s", trashErr, trashStderr)
	} else {
		trashLines := strings.Split(strings.TrimSpace(trashListOutput), "\n")
		for _, line := range trashLines {
			if line != "" {
				// Extract trash ID (first field)
				fields := strings.Fields(line)
				if len(fields) > 0 {
					trashID := fields[0]
					trashItemPath := cephPool + "/" + trashID
					commands = append(commands, fmt.Sprintf("rbd trash remove %s", trashItemPath))
				}
			}
		}
	}

	// Reset OSD full ratio to 0.85
	commands = append(commands, "ceph osd set-full-ratio 0.85")
	// Show final status
	commands = append(commands, "ceph df")

	if len(commands) == 0 {
		fmt.Println("No commands to execute.")
		return
	}

	fmt.Printf("Ceph cleanup for cluster '%s'...\n", clusterName)
	if !execute {
		fmt.Println("The following commands would be executed:")
		for _, command := range commands {
			fmt.Printf("- %s\n", command)
		}
		fmt.Println("\nRun with --execute to perform the cleanup.")
		return
	}

	fmt.Print("This will execute cleanup commands. Are you sure? (yes/no): ")
	response, _ := bufio.NewReader(os.Stdin).ReadString('\n')
	if strings.TrimSpace(strings.ToLower(response)) != "yes" {
		fmt.Println("Cleanup aborted.")
		return
	}

	fmt.Println("Executing cleanup commands...")
	for _, command := range commands {
		fmt.Printf("\nExecuting: %s\n", command)
		stdout, stderr, err := executeInPod(ocpClient, "openshift-storage", toolsPodName, "", []string{"/bin/sh", "-c", command})
		if err != nil {
			log.Printf("Warning: Command failed, but continuing execution: %v\nStderr: %s", err, stderr)
		}
		if stdout != "" {
			fmt.Println(stdout)
		}
	}
	fmt.Println("Cleanup finished.")
}

func generateKubeconfig(cmd *cobra.Command, args []string) {
	clusterName := args[0]

	// Get current working directory
	cwd, err := os.Getwd()
	if err != nil {
		log.Fatalf("%sFailed to get current directory: %v%s", ColorRed, err, ColorReset)
	}

	// Define kubeconfig file path in current directory
	kubeconfigPath := fmt.Sprintf("%s/%s-kubeconfig", cwd, clusterName)

	// Get cluster password
	password, err := getClusterPassword(clusterName)
	if err != nil {
		log.Fatalf("%sCould not get password for cluster %s: %v%s", ColorRed, clusterName, err, ColorReset)
	}

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sGenerating kubeconfig for cluster %s...%s\n", ColorYellow, clusterName, ColorReset)

	// Remove existing kubeconfig if it exists
	if _, err := os.Stat(kubeconfigPath); err == nil {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sRemoving existing kubeconfig file...%s\n", ColorYellow, ColorReset)
		if err := os.Remove(kubeconfigPath); err != nil {
			log.Fatalf("%sFailed to remove existing kubeconfig: %v%s", ColorRed, err, ColorReset)
		}
	}

	// Perform oc login to generate kubeconfig
	apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", clusterName)
	loginCmd := execCommand("oc", "login", "--insecure-skip-tls-verify=true", apiURL, "-u", "kubeadmin", "-p", password, "--kubeconfig", kubeconfigPath)

	output, err := loginCmd.CombinedOutput()
	if err != nil {
		log.Fatalf("%sFailed to generate kubeconfig: %v\nOutput: %s%s", ColorRed, err, string(output), ColorReset)
	}

	// Verify the kubeconfig was created
	if _, err := os.Stat(kubeconfigPath); err != nil {
		log.Fatalf("%sKubeconfig file was not created at %s: %v%s", ColorRed, kubeconfigPath, err, ColorReset)
	}

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s✅ Successfully generated kubeconfig!%s\n", ColorGreen, ColorReset)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sFile location: %s%s\n", ColorCyan, kubeconfigPath, ColorReset)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%sUsage examples:%s\n", ColorCyan, ColorReset)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "  export KUBECONFIG=%s\n", kubeconfigPath)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "  kubectl get nodes --kubeconfig=%s\n", kubeconfigPath)
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "  oc get pods --kubeconfig=%s\n", kubeconfigPath)
}

func createTempResourcesAndGetDf(client *OCPClient) string {
	namespace := "default"
	randomSuffix := strings.ToLower(randomString(6))
	pvcName := "nfs-space-check-pvc-" + randomSuffix
	podName := "nfs-space-check-pod-" + randomSuffix

	// Cleanup function using defer
	defer func() {
		fmt.Println("Cleaning up temporary resources...")

		// Clean up any pods that start with nfs check prefixes
		pods, err := client.KubeClient.CoreV1().Pods(namespace).List(context.TODO(), metav1.ListOptions{})
		if err != nil {
			log.Printf("%sWarning: failed to list pods for cleanup: %v%s", ColorYellow, err, ColorReset)
		} else {
			for _, pod := range pods.Items {
				if strings.HasPrefix(pod.Name, "nfs-df-check-pod-") || strings.HasPrefix(pod.Name, "nfs-space-check-pod-") {
					fmt.Printf("Deleting leftover pod: %s\n", pod.Name)
					err := client.KubeClient.CoreV1().Pods(namespace).Delete(context.TODO(), pod.Name, metav1.DeleteOptions{})
					if err != nil && !errors.IsNotFound(err) {
						log.Printf("%sWarning: failed to delete pod %s: %v%s", ColorYellow, pod.Name, err, ColorReset)
					}
				}
			}
		}

		// Clean up any PVCs that start with nfs check prefixes
		pvcs, err := client.KubeClient.CoreV1().PersistentVolumeClaims(namespace).List(context.TODO(), metav1.ListOptions{})
		if err != nil {
			log.Printf("%sWarning: failed to list PVCs for cleanup: %v%s", ColorYellow, err, ColorReset)
		} else {
			for _, pvc := range pvcs.Items {
				if strings.HasPrefix(pvc.Name, "nfs-df-check-pvc-") || strings.HasPrefix(pvc.Name, "nfs-space-check-pvc-") {
					fmt.Printf("Deleting leftover PVC: %s\n", pvc.Name)
					err := client.KubeClient.CoreV1().PersistentVolumeClaims(namespace).Delete(context.TODO(), pvc.Name, metav1.DeleteOptions{})
					if err != nil && !errors.IsNotFound(err) {
						log.Printf("%sWarning: failed to delete PVC %s: %v%s", ColorYellow, pvc.Name, err, ColorReset)
					}
				}
			}
		}

		fmt.Println("Cleanup complete.")
	}()

	// 1. Create PVC
	fmt.Printf("Creating temporary PVC: %s\n", pvcName)
	storageClassName := "nfs-csi"
	pvc := &v1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: pvcName},
		Spec: v1.PersistentVolumeClaimSpec{
			AccessModes:      []v1.PersistentVolumeAccessMode{v1.ReadWriteOnce},
			StorageClassName: &storageClassName,
			Resources: v1.VolumeResourceRequirements{
				Requests: v1.ResourceList{
					v1.ResourceStorage: resource.MustParse("1Gi"),
				},
			},
		},
	}
	_, err := client.KubeClient.CoreV1().PersistentVolumeClaims(namespace).Create(context.TODO(), pvc, metav1.CreateOptions{})
	if err != nil {
		log.Fatalf("%sFailed to create temporary PVC: %v%s", ColorRed, err, ColorReset)
	}

	// 2. Wait for PVC to be bound
	fmt.Println("Waiting for PVC to be bound...")
	isBound := false
	for i := 0; i < 24; i++ { // Try for 2 minutes (24 * 5 seconds)
		pvcStatus, err := client.KubeClient.CoreV1().PersistentVolumeClaims(namespace).Get(context.TODO(), pvcName, metav1.GetOptions{})
		if err != nil {
			log.Printf("Error checking PVC status: %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		if pvcStatus.Status.Phase == v1.ClaimBound {
			isBound = true
			break
		}
		time.Sleep(5 * time.Second)
	}

	if !isBound {
		log.Fatalf("%sError: Timed out waiting for temporary PVC to be bound.%s", ColorRed, ColorReset)
	}
	fmt.Printf("%sPVC is bound.%s\n", ColorGreen, ColorReset)

	// 3. Create Pod
	fmt.Printf("Creating temporary pod: %s\n", podName)
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: podName},
		Spec: v1.PodSpec{
			Containers: []v1.Container{
				{
					Name:    "inspector",
					Image:   "registry.access.redhat.com/ubi8/ubi-minimal",
					Command: []string{"/bin/sh", "-c", "sleep 3600"},
					VolumeMounts: []v1.VolumeMount{
						{Name: "nfs-volume", MountPath: "/mnt/nfs"},
					},
				},
			},
			Volumes: []v1.Volume{
				{
					Name: "nfs-volume",
					VolumeSource: v1.VolumeSource{
						PersistentVolumeClaim: &v1.PersistentVolumeClaimVolumeSource{
							ClaimName: pvcName,
						},
					},
				},
			},
		},
	}
	_, err = client.KubeClient.CoreV1().Pods(namespace).Create(context.TODO(), pod, metav1.CreateOptions{})
	if err != nil {
		log.Fatalf("%sFailed to create temporary pod: %v%s", ColorRed, err, ColorReset)
	}

	// 4. Wait for Pod to be ready
	fmt.Println("Waiting for pod to be running...")
	isReady := false
	for i := 0; i < 36; i++ { // Try for 3 minutes (36 * 5 seconds)
		podStatus, err := client.KubeClient.CoreV1().Pods(namespace).Get(context.TODO(), podName, metav1.GetOptions{})
		if err != nil {
			log.Printf("%sError checking pod status: %v%s", ColorYellow, err, ColorReset)
			time.Sleep(5 * time.Second)
			continue
		}
		for _, condition := range podStatus.Status.Conditions {
			if condition.Type == v1.PodReady && condition.Status == v1.ConditionTrue {
				isReady = true
				break
			}
		}
		if isReady {
			break
		}
		time.Sleep(5 * time.Second)
	}

	if !isReady {
		log.Fatalf("%sError: Timed out waiting for temporary pod to become ready.%s", ColorRed, ColorReset)
	}
	fmt.Printf("%sPod is running.%s\n", ColorGreen, ColorReset)

	// 5. Exec 'df -h'
	fmt.Printf("Executing 'df -h' in temporary pod '%s'...\n", podName)
	stdout, stderr, err := executeInPod(client, namespace, podName, "inspector", []string{"df", "-h"})
	if err != nil {
		log.Fatalf("%sFailed to execute 'df -h' in temporary pod: %v. Stderr: %s%s", ColorRed, err, stderr, ColorReset)
	}

	return stdout
}

// IIBInfo represents the build information for a specific OCP version
type IIBInfo struct {
	OCPVersion  string `json:"ocp_version"`
	MTVVersion  string `json:"mtv_version"`
	IIB         string `json:"iib"`
	Snapshot    string `json:"snapshot"`
	Created     string `json:"created"`
	Image       string `json:"image"`
	Environment string `json:"environment"`
}

// checkKufloxLogin checks if we're already logged into the kuflox cluster and the right project
var checkKufloxLogin = checkKufloxLoginImpl

func checkKufloxLoginImpl() bool {
	// Check current context
	contextCmd := execCommand("oc", "whoami", "--show-server")
	output, err := contextCmd.CombinedOutput()
	if err != nil {
		return false
	}

	server := strings.TrimSpace(string(output))
	if !strings.Contains(server, "stone-prd-rh01.pg1f.p1.openshiftapps.com") {
		return false
	}

	// Check current project
	projectCmd := execCommand("oc", "project", "-q")
	projectOutput, err := projectCmd.CombinedOutput()
	if err != nil {
		return false
	}

	currentProject := strings.TrimSpace(string(projectOutput))
	return currentProject == "rh-mtv-1-tenant"
}

// loginToKuflox handles automated login to kuflox cluster with SSO support
var loginToKuflox = loginToKufloxImpl

func loginToKufloxImpl() error {
	// Check if user has a valid kerberos ticket (they should run kinit themselves)
	klistCmd := execCommand("klist", "-s")
	hasValidTicket := klistCmd.Run() == nil

	if hasValidTicket {
		_, _ = fmt.Printf("%s✓ Valid kerberos ticket found%s\n", ColorGreen, ColorReset)
		// TODO: Fix SSO authentication with kerberos tickets
		// Currently the SSO authentication is not working properly even with valid kerberos tickets.
		// The issue is that kuflox cluster requires specific SSO configuration that we haven't
		// figured out yet. For now, we fall back to web authentication which works but requires
		// manual browser interaction. This should be revisited to make it seamless for users
		// with valid kerberos tickets.
		// Try SSO-based login using kerberos ticket - use --web flag but with SSO
		_, _ = fmt.Printf("%sTrying SSO authentication...%s\n", ColorYellow, ColorReset)
		loginSSOCmd := execCommand("oc", "login", "--web", "https://api.stone-prd-rh01.pg1f.p1.openshiftapps.com:6443")
		if err := loginSSOCmd.Run(); err == nil {
			_, _ = fmt.Printf("%s✓ SSO authentication successful%s\n", ColorGreen, ColorReset)
			// Switch to the MTV tenant
			projectCmd := execCommand("oc", "project", "rh-mtv-1-tenant")
			if err := projectCmd.Run(); err != nil {
				return fmt.Errorf("failed to switch to rh-mtv-1-tenant: %w", err)
			}
			return nil
		}
		_, _ = fmt.Printf("%sSSO authentication failed, trying other methods...%s\n", ColorYellow, ColorReset)
	} else {
		_, _ = fmt.Printf("%sNo valid kerberos ticket found (run 'kinit' if you want SSO auth)%s\n", ColorYellow, ColorReset)
	}

	// Try to get current token and use it if available
	tokenCmd := execCommand("oc", "whoami", "-t")
	if tokenOutput, err := tokenCmd.CombinedOutput(); err == nil {
		token := strings.TrimSpace(string(tokenOutput))
		if token != "" {
			// Try to login with existing token to the kuflox cluster
			_, _ = fmt.Printf("%sTrying existing token authentication...%s\n", ColorYellow, ColorReset)
			loginTokenCmd := execCommand("oc", "login", "https://api.stone-prd-rh01.pg1f.p1.openshiftapps.com:6443", "--token", token)
			if err := loginTokenCmd.Run(); err == nil {
				_, _ = fmt.Printf("%s✓ Successfully logged in using existing token%s\n", ColorGreen, ColorReset)
				// Switch to the MTV tenant
				projectCmd := execCommand("oc", "project", "rh-mtv-1-tenant")
				if err := projectCmd.Run(); err != nil {
					return fmt.Errorf("failed to switch to rh-mtv-1-tenant: %w", err)
				}
				return nil
			}
			_, _ = fmt.Printf("%sExisting token authentication failed%s\n", ColorYellow, ColorReset)
		}
	}

	// Fall back to web-based authentication
	_, _ = fmt.Printf("%sFalling back to web authentication...%s\n", ColorYellow, ColorReset)
	loginCmd := execCommand("oc", "login", "--web", "https://api.stone-prd-rh01.pg1f.p1.openshiftapps.com:6443")
	if err := loginCmd.Run(); err != nil {
		return fmt.Errorf("failed to login to kuflox cluster: %w", err)
	}

	// Switch to the MTV tenant
	projectCmd := execCommand("oc", "project", "rh-mtv-1-tenant")
	if err := projectCmd.Run(); err != nil {
		return fmt.Errorf("failed to switch to rh-mtv-1-tenant: %w", err)
	}

	return nil
}

// getIIB extracts latest forklift FBC builds from kuflox cluster
func getIIB(cmd *cobra.Command, args []string) {
	if len(args) != 1 {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sError: You must specify an MTV version (e.g., '2.9')%s\n", ColorRed, ColorReset)
		return
	}

	mtvVersion := args[0]
	forceLogin, _ := cmd.Flags().GetBool("force-login")

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sRetrieving MTV %s builds from kuflox cluster...%s\n", ColorYellow, mtvVersion, ColorReset)

	// Check if already logged in to the right cluster (unless force-login is specified)
	if !forceLogin && checkKufloxLogin() {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s✓ Already logged into kuflox cluster (rh-mtv-1-tenant)%s\n", ColorGreen, ColorReset)
	} else {
		if forceLogin {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sForce login requested, re-authenticating...%s\n", ColorYellow, ColorReset)
		} else {
			_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%sConnecting to kuflox cluster...%s\n", ColorYellow, ColorReset)
		}
		if err := loginToKuflox(); err != nil {
			_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sFailed to login to kuflox cluster: %v%s\n", ColorRed, err, ColorReset)
			return
		}
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "%s✓ Successfully connected to kuflox cluster%s\n", ColorGreen, ColorReset)
	}

	// Get production builds
	prodBuilds, err := getForkliftBuilds("prod")
	if err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sFailed to get production builds: %v%s\n", ColorRed, err, ColorReset)
		return
	}

	// Get stage builds
	stageBuilds, err := getForkliftBuilds("stage")
	if err != nil {
		_, _ = fmt.Fprintf(cmd.OutOrStderr(), "%sFailed to get stage builds: %v%s\n", ColorRed, err, ColorReset)
		return
	}

	// Display results
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s=== MTV %s Forklift FBC Builds ===%s\n", ColorCyan, mtvVersion, ColorReset)

	// Production builds
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s📦 PRODUCTION BUILDS:%s\n", ColorGreen, ColorReset)
	for _, build := range prodBuilds {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s  OpenShift %s:%s\n", ColorBlue, build.OCPVersion, ColorReset)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    Full MTV version: %s\n", build.MTVVersion)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    IIB: %s\n", build.IIB)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    OCP version: %s\n", build.OCPVersion)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    Created: %s\n", build.Created)
	}

	// Stage builds
	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s📦 STAGE BUILDS:%s\n", ColorYellow, ColorReset)
	for _, build := range stageBuilds {
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%s  OpenShift %s:%s\n", ColorBlue, build.OCPVersion, ColorReset)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    Full MTV version: %s\n", build.MTVVersion)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    IIB: %s\n", build.IIB)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    OCP version: %s\n", build.OCPVersion)
		_, _ = fmt.Fprintf(cmd.OutOrStdout(), "    Created: %s\n", build.Created)
	}

	_, _ = fmt.Fprintf(cmd.OutOrStdout(), "\n%sSummary: Found %d production and %d stage builds%s\n",
		ColorCyan, len(prodBuilds), len(stageBuilds), ColorReset)
}

// getForkliftBuilds extracts build information for a specific environment (prod/stage)
var getForkliftBuilds = getForkliftBuildsImpl

func getForkliftBuildsImpl(environment string) ([]IIBInfo, error) {
	// Create kuflox client
	client, err := createKufloxClient()
	if err != nil {
		return nil, fmt.Errorf("failed to create kuflox client: %w", err)
	}

	var builds []IIBInfo

	// Get snapshots for the specific environment and extract build info
	for _, version := range []string{"417", "418", "419"} {
		build, err := getLatestBuildForVersionWithClient(client, environment, version)
		if err != nil {
			// Silently continue with other versions - don't print warnings that can interfere with TUI
			continue
		}
		if build != nil {
			builds = append(builds, *build)
		}
	}

	return builds, nil
}

// getLatestBuildForVersionWithClient gets the latest build using the Go client instead of oc commands
func getLatestBuildForVersionWithClient(client dynamic.Interface, environment, version string) (*IIBInfo, error) {
	// Define the snapshot resource
	snapshotGVR := schema.GroupVersionResource{
		Group:    "appstudio.redhat.com",
		Version:  "v1alpha1",
		Resource: "snapshots",
	}

	// Get all snapshots in the rh-mtv-1-tenant namespace
	snapshots, err := client.Resource(snapshotGVR).Namespace("rh-mtv-1-tenant").List(context.TODO(), metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("failed to list snapshots: %w", err)
	}

	// Filter snapshots for the specific environment and version
	var matchingSnapshots []unstructured.Unstructured
	targetApp := fmt.Sprintf("forklift-fbc-%s-v%s", environment, version)

	for _, snapshot := range snapshots.Items {
		// Check if the application label matches
		if labels := snapshot.GetLabels(); labels != nil {
			if app, exists := labels["appstudio.openshift.io/application"]; exists && app == targetApp {
				matchingSnapshots = append(matchingSnapshots, snapshot)
			}
		}
	}

	if len(matchingSnapshots) == 0 {
		return nil, fmt.Errorf("no snapshots found for %s v%s", environment, version)
	}

	// Sort by creation timestamp to get the latest
	sort.Slice(matchingSnapshots, func(i, j int) bool {
		return matchingSnapshots[i].GetCreationTimestamp().After(matchingSnapshots[j].GetCreationTimestamp().Time)
	})

	latest := matchingSnapshots[0]

	// Extract the required information
	name := latest.GetName()
	created := latest.GetCreationTimestamp().Local().Format("2006-01-02 15:04:05 MST")

	// Extract container image from spec.components[0].containerImage
	var image string
	if components, found, err := unstructured.NestedSlice(latest.Object, "spec", "components"); err == nil && found && len(components) > 0 {
		if component, ok := components[0].(map[string]interface{}); ok {
			if containerImage, found, err := unstructured.NestedString(component, "containerImage"); err == nil && found {
				image = containerImage
			}
		}
	}

	// Extract git revision from spec.components[0].source.git.revision
	var revision string
	if components, found, err := unstructured.NestedSlice(latest.Object, "spec", "components"); err == nil && found && len(components) > 0 {
		if component, ok := components[0].(map[string]interface{}); ok {
			if gitRevision, found, err := unstructured.NestedString(component, "source", "git", "revision"); err == nil && found {
				revision = gitRevision
			}
		}
	}

	// If revision is empty, use snapshot name suffix as fallback
	if revision == "" {
		parts := strings.Split(name, "-")
		if len(parts) > 0 {
			revision = parts[len(parts)-1]
		} else {
			revision = "unknown"
		}
	}

	// Format OCP version (417 -> 4.17)
	ocpVersion := fmt.Sprintf("4.%s", version[1:])

	// Create IIB in the required format: forklift-fbc-prod-v417:on-pr-<git-hash>
	iib := fmt.Sprintf("forklift-fbc-%s-v%s:on-pr-%s", environment, version, revision)

	build := &IIBInfo{
		OCPVersion:  ocpVersion,
		MTVVersion:  "2.9", // Currently all builds are MTV 2.9
		IIB:         iib,
		Snapshot:    name,
		Created:     created,
		Image:       image,
		Environment: environment,
	}

	return build, nil
}

// createKufloxClient creates a Kubernetes client for the kuflox cluster using the current token
func createKufloxClient() (dynamic.Interface, error) {
	// Get current token
	tokenCmd := execCommand("oc", "whoami", "-t")
	tokenOutput, err := tokenCmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("failed to get current token: %w", err)
	}

	token := strings.TrimSpace(string(tokenOutput))
	if token == "" {
		return nil, fmt.Errorf("no valid token found")
	}

	// Create REST config for kuflox cluster
	config := &rest.Config{
		Host:        "https://api.stone-prd-rh01.pg1f.p1.openshiftapps.com:6443",
		BearerToken: token,
		TLSClientConfig: rest.TLSClientConfig{
			Insecure: true, // Usually kuflox uses valid certs, but keeping flexible
		},
	}

	// Create dynamic client
	dynamicClient, err := dynamic.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create dynamic client: %w", err)
	}

	return dynamicClient, nil
}
