package main

import (
	"bufio"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	configv1 "github.com/openshift/client-go/config/clientset/versioned/typed/config/v1"
	routev1 "github.com/openshift/client-go/route/clientset/versioned/typed/route/v1"
	"github.com/spf13/cobra"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// Color constants for output formatting
const (
	ColorReset  = "\033[0m"
	ColorRed    = "\033[31m"
	ColorGreen  = "\033[32m"
	ColorYellow = "\033[33m"
	ColorBlue   = "\033[34m"
	ColorPurple = "\033[35m"
	ColorCyan   = "\033[36m"
	ColorWhite  = "\033[37m"
)

// GoProviderConfig represents provider configuration
type GoProviderConfig struct {
	Type    string
	Version string
}

// ProviderConfig represents provider connection details
type ProviderConfig struct {
	Type        string            // Provider type (vmware, ovirt, openstack)
	URL         string            // API endpoint URL
	Username    string            // Username for authentication
	Password    string            // Password for authentication
	Insecure    bool              // Skip TLS verification
	ExtraParams map[string]string // Additional provider-specific parameters
}

// RunTemplateConfig represents run template configuration
type RunTemplateConfig struct {
	Provider string
	Storage  string
	Remote   bool
}

// OCPClient aggregates the Kubernetes and OpenShift clients.
type OCPClient struct {
	KubeClient    kubernetes.Interface
	ConfigClient  configv1.ConfigV1Interface
	RouteClient   routev1.RouteV1Interface
	DynamicClient dynamic.Interface
	RESTConfig    *rest.Config
}

// ClusterInfo holds cluster information
type ClusterInfo struct {
	Name       string
	OCPVersion string
	MTVVersion string
	CNVVersion string
	IIB        string
	ConsoleURL string
}

// CmdRunner is a minimal interface for exec commands
// Used for testability in CLI tests
type CmdRunner interface {
	CombinedOutput() ([]byte, error)
	Run() error
}

// Global variables - these need to be in a single file to avoid redeclaration
var (
	ocpClient     *OCPClient
	rootCmd       = &cobra.Command{Use: "mtv-dev", Short: "A CLI for MTV API test development"}
	full          bool
	CLUSTERS_PATH = "/mnt/cnv-qe.rhcloud.com"
	randSrc       = rand.NewSource(time.Now().UnixNano())
	randGen       = rand.New(randSrc)
)

// Provider and storage configurations
var providerMap = map[string]GoProviderConfig{
	"vmware6":   {"vsphere", "6.5"},
	"vmware7":   {"vsphere", "7.0.3"},
	"vmware8":   {"vsphere", "8.0.1"},
	"ovirt":     {"ovirt", "4.4.9"},
	"openstack": {"openstack", "psi"},
	"ova":       {"ova", "nfs"},
}

var storageMap = map[string]string{
	"ceph": "ocs-storagecluster-ceph-rbd",
	"nfs":  "nfs-csi",
	"csi":  "standard-csi",
}

var runsTemplates = map[string]RunTemplateConfig{
	"vmware6-csi":         {"vmware6", "csi", false},
	"vmware6-csi-remote":  {"vmware6", "csi", true},
	"vmware7-ceph":        {"vmware7", "ceph", false},
	"vmware7-ceph-remote": {"vmware7", "ceph", true},
	"vmware8-ceph-remote": {"vmware8", "ceph", true},
	"vmware8-nfs":         {"vmware8", "nfs", false},
	"vmware8-csi":         {"vmware8", "csi", false},
	"openstack-ceph":      {"openstack", "ceph", false},
	"openstack-csi":       {"openstack", "csi", false},
	"ovirt-ceph":          {"ovirt", "ceph", false},
	"ovirt-csi":           {"ovirt", "csi", false},
	"ovirt-csi-remote":    {"ovirt", "csi", true},
	"ova-ceph":            {"ova", "ceph", false},
}

// loadProviderConfigs loads VM provider configurations from the Python config file
func loadProviderConfigs() (map[string]ProviderConfig, error) {
	// Find the Python config file by searching up from the executable location
	configPath, err := findConfigFile()
	if err != nil {
		return nil, err
	}

	file, err := os.Open(configPath)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()

	providerConfigs := make(map[string]ProviderConfig)
	scanner := bufio.NewScanner(file)

	// Regex patterns to extract provider data
	providerKeyRegex := regexp.MustCompile(`^\s*"([^"]+)": \{`)
	typeRegex := regexp.MustCompile(`^\s*"type": "([^"]+)",?`)
	apiURLRegex := regexp.MustCompile(`^\s*"api_url": "([^"]+)",?`)
	usernameRegex := regexp.MustCompile(`^\s*"username": "([^"]+)",?`)
	passwordRegex := regexp.MustCompile(`^\s*"password": "([^"]+)",?`)
	projectNameRegex := regexp.MustCompile(`^\s*"project_name": "([^"]+)",?`)
	regionNameRegex := regexp.MustCompile(`^\s*"region_name": "([^"]+)",?`)
	userDomainNameRegex := regexp.MustCompile(`^\s*"user_domain_name": "([^"]+)",?`)
	userDomainIDRegex := regexp.MustCompile(`^\s*"user_domain_id": "([^"]+)",?`)
	projectDomainIDRegex := regexp.MustCompile(`^\s*"project_domain_id": "([^"]+)",?`)
	closingBraceRegex := regexp.MustCompile(`^\s*\},?\s*$`)

	var currentProvider string
	var currentConfig ProviderConfig
	inSourceProviders := false
	braceDepth := 0

	for scanner.Scan() {
		line := scanner.Text()

		// Check if we're entering the source_providers_dict
		if strings.Contains(line, "source_providers_dict") && strings.Contains(line, "{") {
			inSourceProviders = true
			braceDepth = 1
			continue
		}

		if !inSourceProviders {
			continue
		}

		// Track brace depth to know when we exit the main dict
		openBraces := strings.Count(line, "{")
		closeBraces := strings.Count(line, "}")
		braceDepth += openBraces - closeBraces

		// If brace depth reaches 0, we've exited the source_providers_dict
		if braceDepth <= 0 {
			// Save the last provider before exiting
			if currentProvider != "" && currentConfig.Type != "" {
				if currentConfig.Type != "ova" && currentConfig.Type != "openshift" {
					providerConfigs[currentProvider] = currentConfig
				}
			}
			break
		}

		// Look for provider key (e.g., "vsphere-7.0.3": {)
		if matches := providerKeyRegex.FindStringSubmatch(line); len(matches) > 1 {
			// Save previous provider if it exists
			if currentProvider != "" && currentConfig.Type != "" {
				// Skip OVA and OpenShift providers
				if currentConfig.Type != "ova" && currentConfig.Type != "openshift" {
					providerConfigs[currentProvider] = currentConfig
				}
			}

			// Start new provider
			currentProvider = matches[1]
			currentConfig = ProviderConfig{
				Insecure:    true, // Default to true for development environments
				ExtraParams: make(map[string]string),
			}
			continue
		}

		// Check for closing brace of current provider
		if closingBraceRegex.MatchString(line) && currentProvider != "" {
			// Save current provider
			if currentConfig.Type != "" {
				if currentConfig.Type != "ova" && currentConfig.Type != "openshift" {
					providerConfigs[currentProvider] = currentConfig
				}
			}
			// Reset for next provider
			currentProvider = ""
			currentConfig = ProviderConfig{}
			continue
		}

		if currentProvider == "" {
			continue
		}

		// Extract provider properties
		if matches := typeRegex.FindStringSubmatch(line); len(matches) > 1 {
			providerType := matches[1]
			// Map Python types to our provider types
			switch providerType {
			case "vsphere":
				currentConfig.Type = "vmware"
			case "ovirt":
				currentConfig.Type = "ovirt"
			case "openstack":
				currentConfig.Type = "openstack"
			default:
				currentConfig.Type = providerType
			}
		} else if matches := apiURLRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.URL = matches[1]
		} else if matches := usernameRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.Username = matches[1]
		} else if matches := passwordRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.Password = matches[1]
		} else if matches := projectNameRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.ExtraParams["tenant_name"] = matches[1]
		} else if matches := regionNameRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.ExtraParams["region"] = matches[1]
		} else if matches := userDomainNameRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.ExtraParams["user_domain_name"] = matches[1]
		} else if matches := userDomainIDRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.ExtraParams["user_domain_id"] = matches[1]
		} else if matches := projectDomainIDRegex.FindStringSubmatch(line); len(matches) > 1 {
			currentConfig.ExtraParams["project_domain_id"] = matches[1]
		}
	}

	return providerConfigs, scanner.Err()
}

// findConfigFile searches for the Python config file starting from the executable location
// and walking up the directory tree until it finds the mtv-api-tests project root
func findConfigFile() (string, error) {
	// Get the executable path
	execPath, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("failed to get executable path: %v", err)
	}

	// Get the directory containing the executable
	execDir := filepath.Dir(execPath)

	// Start searching from the executable directory and walk up
	currentDir := execDir
	for {
		// Check if we've found the mtv-api-tests project root by looking for specific identifiers
		mtvProjectMarkers := []string{
			"libs/base_provider.py",      // Unique to mtv-api-tests
			"utilities/mtv_migration.py", // Unique to mtv-api-tests
			"conftest.py",                // Common in mtv-api-tests root
			"OWNERS",                     // Specific to mtv-api-tests
		}

		foundMarkers := 0
		for _, marker := range mtvProjectMarkers {
			if _, err := os.Stat(filepath.Join(currentDir, marker)); err == nil {
				foundMarkers++
			}
		}

		// If we found multiple MTV-specific markers, this is likely the mtv-api-tests project root
		if foundMarkers >= 2 {
			configPath := filepath.Join(currentDir, "tests", "tests_config", "config.py")
			if _, err := os.Stat(configPath); err == nil {
				// Validate that this config file contains mtv-api-tests specific content
				if isValidMTVConfig(configPath) {
					return configPath, nil
				}
			}
		}

		// Also check if we're in the tools/dev directory and look relatively
		if strings.HasSuffix(currentDir, "tools/dev") {
			// Check if this is inside an mtv-api-tests project
			parentDir := filepath.Dir(filepath.Dir(currentDir)) // Go up two levels
			configPath := filepath.Join(parentDir, "tests", "tests_config", "config.py")
			if _, err := os.Stat(configPath); err == nil {
				// Validate MTV-specific markers in the parent directory
				mtvMarkerFound := false
				for _, marker := range mtvProjectMarkers {
					if _, err := os.Stat(filepath.Join(parentDir, marker)); err == nil {
						mtvMarkerFound = true
						break
					}
				}
				if mtvMarkerFound && isValidMTVConfig(configPath) {
					return configPath, nil
				}
			}
		}

		// Move up one directory
		parentDir := filepath.Dir(currentDir)
		if parentDir == currentDir {
			// Reached filesystem root, stop searching
			break
		}
		currentDir = parentDir
	}

	// If not found by walking up, try some common locations relative to working directory
	wd, err := os.Getwd()
	if err == nil {
		// Try from current working directory
		candidatePaths := []string{
			filepath.Join(wd, "tests", "tests_config", "config.py"),
			filepath.Join(wd, "..", "..", "tests", "tests_config", "config.py"),
			filepath.Join(wd, "..", "tests", "tests_config", "config.py"),
		}

		for _, path := range candidatePaths {
			if _, err := os.Stat(path); err == nil {
				if isValidMTVConfig(path) {
					return path, nil
				}
			}
		}
	}

	// Try environment variable if set
	if envPath := os.Getenv("MTV_CONFIG_PATH"); envPath != "" {
		if _, err := os.Stat(envPath); err == nil {
			return envPath, nil // Trust user's explicit path
		}
	}

	return "", fmt.Errorf("could not find mtv-api-tests Python config file. Searched from executable path %s and working directory %s. You can set MTV_CONFIG_PATH environment variable to specify the config file location", execPath, wd)
}

// isValidMTVConfig validates that the config file contains MTV-specific content
func isValidMTVConfig(configPath string) bool {
	file, err := os.Open(configPath)
	if err != nil {
		return false
	}
	defer func() { _ = file.Close() }()

	scanner := bufio.NewScanner(file)

	// Look for MTV-specific indicators in the config file
	mtvIndicators := []string{
		"source_providers_dict", // MTV-specific config structure
		"vsphere",               // MTV uses vSphere providers
		"ovirt",                 // MTV uses oVirt providers
		"openstack",             // MTV uses OpenStack providers
	}

	foundIndicators := 0
	for scanner.Scan() {
		line := scanner.Text()
		for _, indicator := range mtvIndicators {
			if strings.Contains(line, indicator) {
				foundIndicators++
				break // Only count each indicator once
			}
		}

		// If we found multiple indicators, this is likely the correct config
		if foundIndicators >= 2 {
			return true
		}
	}

	return foundIndicators >= 2
}
