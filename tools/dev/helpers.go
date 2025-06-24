package main

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/atotto/clipboard"
)

// Replace direct getClusterPassword function with a variable for testability
var getClusterPassword = getClusterPasswordImpl

func getClusterPasswordImpl(clusterName string) (string, error) {
	passwordPath := fmt.Sprintf("%s/%s/auth/kubeadmin-password", CLUSTERS_PATH, clusterName)
	data, err := os.ReadFile(passwordPath)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}

// For testability, allow mocking of ensureLoggedIn
var ensureLoggedIn = ensureLoggedInImpl
var ensureLoggedInSilent = ensureLoggedInSilentImpl

func ensureLoggedInImpl(clusterName string) error {
	// Test connectivity by trying to build an OCP client using cluster-specific kubeconfig
	fmt.Printf("%sLogging in to %s...%s\n", ColorYellow, clusterName, ColorReset)

	_, err := buildOCPClient(clusterName)
	if err != nil {
		return fmt.Errorf("failed to connect to cluster %s: %w", clusterName, err)
	}

	return nil
}

func ensureLoggedInSilentImpl(clusterName string) error {
	// Test connectivity by trying to build an OCP client using cluster-specific kubeconfig
	// Silent version - no progress messages

	_, err := buildOCPClient(clusterName)
	if err != nil {
		return fmt.Errorf("failed to connect to cluster %s: %w", clusterName, err)
	}

	return nil
}

// For testability, allow mocking of clipboard.WriteAll
var clipboardWriteAll = clipboard.WriteAll

// realExecCommand wraps exec.Command to return a CmdRunner
func realExecCommand(name string, args ...string) CmdRunner {
	return exec.Command(name, args...)
}

// For testability, allow mocking of execCommand
var execCommand = realExecCommand

var getClusterVersion = getClusterVersionImpl

func getClusterVersionImpl(clusterName string) (string, error) {
	client, err := buildOCPClient(clusterName)
	if err != nil {
		return "", fmt.Errorf("failed to connect to cluster %s: %w", clusterName, err)
	}

	// Get Kubernetes server version which should be accessible with basic auth
	serverVersion, err := client.KubeClient.Discovery().ServerVersion()
	if err != nil {
		return "", fmt.Errorf("failed to get server version: %w", err)
	}

	// Convert Kubernetes version to OpenShift-style format
	// Kubernetes 1.24.x typically corresponds to OpenShift 4.11.x
	// Kubernetes 1.25.x typically corresponds to OpenShift 4.12.x
	// Kubernetes 1.26.x typically corresponds to OpenShift 4.13.x
	// etc.
	parts := strings.Split(serverVersion.GitVersion, ".")
	if len(parts) >= 2 {
		majorMinor := strings.TrimPrefix(parts[0], "v") + "." + parts[1]
		switch {
		case strings.HasPrefix(majorMinor, "1.24"):
			return "4.11", nil
		case strings.HasPrefix(majorMinor, "1.25"):
			return "4.12", nil
		case strings.HasPrefix(majorMinor, "1.26"):
			return "4.13", nil
		case strings.HasPrefix(majorMinor, "1.27"):
			return "4.14", nil
		case strings.HasPrefix(majorMinor, "1.28"):
			return "4.15", nil
		case strings.HasPrefix(majorMinor, "1.29"):
			return "4.16", nil
		case strings.HasPrefix(majorMinor, "1.30"):
			return "4.17", nil
		case strings.HasPrefix(majorMinor, "1.31"):
			return "4.18", nil
		case strings.HasPrefix(majorMinor, "1.32"):
			return "4.19", nil
		default:
			// Default mapping for newer versions
			return "4.19", nil
		}
	}

	return "", fmt.Errorf("unable to parse server version: %s", serverVersion.GitVersion)
}

func randomString(n int) string {
	var letters = []rune("abcdefghijklmnopqrstuvwxyz0123456789")
	b := make([]rune, n)
	for i := range b {
		b[i] = letters[randGen.Intn(len(letters))]
	}
	return string(b)
}

func ensureNfsMounted() error {
	if _, err := os.Stat(CLUSTERS_PATH); os.IsNotExist(err) {
		fmt.Println("Clusters directory not found, attempting to create and mount with sudo...")
		sudoMkdir := exec.Command("sudo", "mkdir", "-p", CLUSTERS_PATH)
		if err := sudoMkdir.Run(); err != nil {
			return fmt.Errorf("failed to create mount point %s: %w", CLUSTERS_PATH, err)
		}
		mountCmd := exec.Command("sudo", "mount", "-t", "nfs", "10.9.96.21:/rhos_psi_cluster_dirs", CLUSTERS_PATH)
		if err := mountCmd.Run(); err != nil {
			return fmt.Errorf("failed to mount NFS: %w", err)
		}
		fmt.Println("NFS mounted successfully.")
	}
	return nil
}
