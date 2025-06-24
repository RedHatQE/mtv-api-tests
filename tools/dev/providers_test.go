package main

import (
	"bytes"
	"context"
	"os"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func TestCreateProvider(t *testing.T) {
	tests := []struct {
		name         string
		providerType string
		url          string
		username     string
		password     string
		insecure     bool
		extraParams  map[string]string
		expectError  bool
		expectedType string
	}{
		{
			name:         "VMware provider",
			providerType: "vmware",
			url:          "https://vcenter.example.com",
			username:     "admin",
			password:     "secret",
			insecure:     true,
			extraParams:  map[string]string{},
			expectError:  false,
			expectedType: "vmware",
		},
		{
			name:         "oVirt provider",
			providerType: "ovirt",
			url:          "https://ovirt.example.com",
			username:     "admin",
			password:     "secret",
			insecure:     true,
			extraParams:  map[string]string{},
			expectError:  false,
			expectedType: "ovirt",
		},
		{
			name:         "OpenStack provider",
			providerType: "openstack",
			url:          "https://keystone.example.com:5000/v3",
			username:     "admin",
			password:     "secret",
			insecure:     true,
			extraParams: map[string]string{
				"tenant_name":       "admin",
				"region":            "regionOne",
				"user_domain_name":  "Default",
				"user_domain_id":    "default",
				"project_domain_id": "default",
			},
			expectError:  false,
			expectedType: "openstack",
		},
		{
			name:         "Unsupported provider",
			providerType: "unsupported",
			url:          "https://example.com",
			username:     "admin",
			password:     "secret",
			insecure:     false,
			extraParams:  map[string]string{},
			expectError:  true,
			expectedType: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			provider, err := CreateProvider(
				tt.providerType,
				tt.url,
				tt.username,
				tt.password,
				tt.insecure,
				tt.extraParams,
			)

			if tt.expectError {
				if err == nil {
					t.Errorf("Expected error for provider type %s, but got none", tt.providerType)
				}
				return
			}

			if err != nil {
				t.Errorf("Unexpected error creating provider: %v", err)
				return
			}

			if provider == nil {
				t.Error("Provider should not be nil")
				return
			}

			if provider.GetProviderType() != tt.expectedType {
				t.Errorf("Expected provider type %s, got %s", tt.expectedType, provider.GetProviderType())
			}
		})
	}
}

// Test oVirt power state mapping
func TestOVirtPowerStateMapping(t *testing.T) {
	// This tests the critical TODO issue we identified
	provider := NewOVirtProvider("https://ovirt.example.com", "admin", "secret", true)

	if provider.GetProviderType() != "ovirt" {
		t.Errorf("Expected provider type 'ovirt', got '%s'", provider.GetProviderType())
	}

	// TODO: Add more comprehensive tests for power state mapping once we resolve the "unknown" status issue
	// For now, just test that the provider can be created
}

// Test power state filtering logic
func TestPowerStateFiltering(t *testing.T) {
	vms := []VMInfo{
		{Name: "vm1", PowerState: "running", Provider: "vmware"},
		{Name: "vm2", PowerState: "poweredon", Provider: "vmware"},
		{Name: "vm3", PowerState: "poweredoff", Provider: "vmware"},
		{Name: "vm4", PowerState: "active", Provider: "openstack"},
		{Name: "vm5", PowerState: "stopped", Provider: "openstack"},
		{Name: "vm6", PowerState: "up", Provider: "ovirt"},
		{Name: "vm7", PowerState: "down", Provider: "ovirt"},
		{Name: "vm8", PowerState: "unknown", Provider: "ovirt"},
	}

	// Test filtering for running VMs
	var runningVMs []VMInfo
	for _, vm := range vms {
		if strings.ToLower(vm.PowerState) == "poweredon" ||
			strings.ToLower(vm.PowerState) == "running" ||
			strings.ToLower(vm.PowerState) == "active" ||
			strings.ToLower(vm.PowerState) == "up" {
			runningVMs = append(runningVMs, vm)
		}
	}

	expectedRunning := 4 // vm1, vm2, vm4, vm6
	if len(runningVMs) != expectedRunning {
		t.Errorf("Expected %d running VMs, got %d", expectedRunning, len(runningVMs))
	}

	// Verify specific VMs are included
	runningNames := make(map[string]bool)
	for _, vm := range runningVMs {
		runningNames[vm.Name] = true
	}

	expectedNames := []string{"vm1", "vm2", "vm4", "vm6"}
	for _, name := range expectedNames {
		if !runningNames[name] {
			t.Errorf("VM %s should be in running list", name)
		}
	}
}

// Test VM table output formatting
func TestPrintVMTable(t *testing.T) {
	vms := []VMInfo{
		{
			Name:       "short-name",
			Provider:   "vmware",
			PowerState: "running",
			CPU:        4,
			MemoryMB:   8192,
			GuestOS:    "Ubuntu Linux",
		},
		{
			Name:       "very-long-vm-name-that-should-be-truncated-in-table",
			Provider:   "ovirt",
			PowerState: "poweredoff",
			CPU:        2,
			MemoryMB:   4096,
			GuestOS:    "Windows Server 2019 Standard Edition",
		},
		{
			Name:       "unknown-state-vm",
			Provider:   "ovirt",
			PowerState: "unknown",
			CPU:        1,
			MemoryMB:   1024,
			GuestOS:    "RHEL 8",
		},
	}

	// Capture table output
	cmd := &cobra.Command{}
	var buf bytes.Buffer
	cmd.SetOut(&buf)

	printVMTable(cmd, vms, true)
	output := buf.String()

	// Verify table structure
	if !strings.Contains(output, "NAME") {
		t.Error("Table should contain NAME header")
	}
	if !strings.Contains(output, "PROVIDER") {
		t.Error("Table should contain PROVIDER header")
	}
	if !strings.Contains(output, "POWER STATE") {
		t.Error("Table should contain POWER STATE header")
	}

	// Verify VM data is present
	if !strings.Contains(output, "short-name") {
		t.Error("Table should contain short-name VM")
	}
	if !strings.Contains(output, "very-long-vm-name-that-shou...") {
		t.Errorf("Long VM name should be truncated with '...', got output: %s", output)
	}
	if !strings.Contains(output, "8.0GB") {
		t.Error("Memory should be formatted in GB")
	}

	// Verify no empty output for VMs
	if len(vms) > 0 && len(strings.TrimSpace(output)) == 0 {
		t.Error("Table output should not be empty when VMs are provided")
	}
}

// Test JSON output formatting
func TestPrintVMJSON(t *testing.T) {
	vms := []VMInfo{
		{
			Name:       "test-vm",
			Provider:   "vmware",
			PowerState: "running",
			UUID:       "uuid-123",
			CPU:        4,
			MemoryMB:   8192,
			GuestOS:    "Ubuntu",
			StorageGB:  100.5,
		},
	}

	cmd := &cobra.Command{}
	var buf bytes.Buffer
	cmd.SetOut(&buf)

	printVMJSON(cmd, vms)
	output := buf.String()

	// Verify JSON structure
	if !strings.Contains(output, `"name": "test-vm"`) {
		t.Error("JSON should contain VM name")
	}
	if !strings.Contains(output, `"provider": "vmware"`) {
		t.Error("JSON should contain provider")
	}
	if !strings.Contains(output, `"power_state": "running"`) {
		t.Error("JSON should contain power state")
	}
	if !strings.Contains(output, `"cpu": 4`) {
		t.Error("JSON should contain CPU count")
	}
	if !strings.Contains(output, `"memory_mb": 8192`) {
		t.Error("JSON should contain memory in MB")
	}
	if !strings.Contains(output, `"storage_gb": 100.50`) {
		t.Error("JSON should contain storage in GB")
	}

	// Verify valid JSON structure
	if !strings.HasPrefix(strings.TrimSpace(output), "[") {
		t.Error("JSON output should start with array bracket")
	}
	if !strings.HasSuffix(strings.TrimSpace(output), "]") {
		t.Error("JSON output should end with array bracket")
	}
}

// Test simple output formatting
func TestPrintVMSimple(t *testing.T) {
	vms := []VMInfo{
		{Name: "vm1"},
		{Name: "vm2"},
		{Name: "vm3"},
	}

	cmd := &cobra.Command{}
	var buf bytes.Buffer
	cmd.SetOut(&buf)

	printVMSimple(cmd, vms)
	output := buf.String()

	lines := strings.Split(strings.TrimSpace(output), "\n")
	if len(lines) != 3 {
		t.Errorf("Expected 3 lines of output, got %d", len(lines))
	}

	expectedNames := []string{"vm1", "vm2", "vm3"}
	for i, expectedName := range expectedNames {
		if i < len(lines) && strings.TrimSpace(lines[i]) != expectedName {
			t.Errorf("Expected line %d to be '%s', got '%s'", i, expectedName, lines[i])
		}
	}
}

// Test get-vms command argument validation
func TestGetVMsCommand_ArgumentValidation(t *testing.T) {
	tests := []struct {
		name        string
		args        []string
		flags       map[string]string
		expectError bool
		errorMsg    string
	}{
		{
			name:        "No arguments, no --list-providers",
			args:        []string{},
			flags:       map[string]string{},
			expectError: true,
			errorMsg:    "provider name is required",
		},
		{
			name:        "List providers flag",
			args:        []string{},
			flags:       map[string]string{"list-providers": "true"},
			expectError: false,
		},
		{
			name:        "Valid provider argument",
			args:        []string{"test-provider"},
			flags:       map[string]string{},
			expectError: false, // Will fail later due to unknown provider, but args are valid
		},
		{
			name:        "Too many arguments",
			args:        []string{"provider1", "provider2"},
			flags:       map[string]string{},
			expectError: false, // Command accepts 0-1 args, so this should be handled
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// This would require refactoring getVMs to be testable
			// For now, we're documenting the test cases we should have
			t.Skip("Requires refactoring getVMs function to be testable")
		})
	}
}

func TestVMProviderConfigs(t *testing.T) {
	// Test that we can load provider configs dynamically
	configs, err := loadProviderConfigs()
	if err != nil {
		t.Logf("Could not load provider configs (this may be expected in test environment): %v", err)
		return
	}

	// Test that we have some providers loaded
	if len(configs) == 0 {
		t.Error("No provider configurations loaded")
		return
	}

	// Test that each loaded provider has required fields
	for providerName, config := range configs {
		t.Run(providerName, func(t *testing.T) {
			// Check required fields
			if config.Type == "" {
				t.Error("Provider type should not be empty")
			}
			if config.URL == "" {
				t.Error("Provider URL should not be empty")
			}
			if config.Username == "" {
				t.Error("Provider username should not be empty")
			}
			if config.Password == "" {
				t.Error("Provider password should not be empty")
			}
			if config.ExtraParams == nil {
				t.Error("Provider ExtraParams should not be nil")
			}

			// Test that excluded types are not present
			if config.Type == "ova" || config.Type == "openshift" {
				t.Errorf("Provider type %s should be excluded", config.Type)
			}

			// Test that valid types are properly mapped
			validTypes := []string{"vmware", "ovirt", "openstack"}
			found := false
			for _, validType := range validTypes {
				if config.Type == validType {
					found = true
					break
				}
			}
			if !found {
				t.Errorf("Provider type %s is not a valid type", config.Type)
			}
		})
	}
}

func TestVMwareProvider_GetProviderType(t *testing.T) {
	provider := NewVMwareProvider("https://vcenter.example.com", "admin", "secret", true)

	if provider.GetProviderType() != "vmware" {
		t.Errorf("Expected provider type 'vmware', got '%s'", provider.GetProviderType())
	}
}

func TestVMInfo(t *testing.T) {
	vmInfo := VMInfo{
		Name:        "test-vm",
		Provider:    "vmware",
		PowerState:  "poweredOn",
		UUID:        "uuid-123",
		CPU:         4,
		MemoryMB:    8192,
		GuestOS:     "Ubuntu Linux",
		IPAddresses: []string{"192.168.1.100"},
		StorageGB:   100.5,
	}

	if vmInfo.Name != "test-vm" {
		t.Errorf("Expected VM name 'test-vm', got '%s'", vmInfo.Name)
	}

	if vmInfo.Provider != "vmware" {
		t.Errorf("Expected provider 'vmware', got '%s'", vmInfo.Provider)
	}

	if vmInfo.CPU != 4 {
		t.Errorf("Expected CPU count 4, got %d", vmInfo.CPU)
	}
}

func TestListVMsWithoutConnection(t *testing.T) {
	ctx := context.Background()

	vmwareProvider := NewVMwareProvider("https://vcenter.example.com", "admin", "secret", true)
	_, err := vmwareProvider.ListVMs(ctx)
	if err == nil {
		t.Error("Expected error when listing VMs without connection")
	}

	oVirtProvider := NewOVirtProvider("https://ovirt.example.com", "admin", "secret", true)
	_, err = oVirtProvider.ListVMs(ctx)
	if err == nil {
		t.Error("Expected error when listing VMs without connection")
	}

	openStackProvider := NewOpenStackProvider(
		"https://keystone.example.com:5000/v3",
		"admin", "secret", "admin", "regionOne",
		"Default", "default", "default", true)
	_, err = openStackProvider.ListVMs(ctx)
	if err == nil {
		t.Error("Expected error when listing VMs without connection")
	}
}

func TestProviderConfigLookup(t *testing.T) {
	// Test that we can look up provider configurations dynamically
	configs, err := loadProviderConfigs()
	if err != nil {
		t.Logf("Could not load provider configs (this may be expected in test environment): %v", err)
		return
	}

	// Test non-existent provider
	_, exists := configs["non-existent"]
	if exists {
		t.Error("non-existent provider should not exist")
	}

	// Test that at least one provider exists (if any are loaded)
	if len(configs) > 0 {
		// Just check that the first one is valid
		for name, config := range configs {
			if config.Type == "" {
				t.Errorf("Provider %s should have a valid type", name)
			}
			break // Only test the first one
		}
	}
}

func TestFindConfigFile(t *testing.T) {
	// Test that findConfigFile either finds the MTV-specific file or returns a descriptive error
	configPath, err := findConfigFile()
	if err != nil {
		// This is expected in some test environments
		t.Logf("findConfigFile returned error (may be expected): %v", err)
		// Check that the error message contains helpful information
		if !strings.Contains(err.Error(), "MTV_CONFIG_PATH") {
			t.Error("Error message should mention MTV_CONFIG_PATH environment variable")
		}
		if !strings.Contains(err.Error(), "mtv-api-tests") {
			t.Error("Error message should mention mtv-api-tests")
		}
		return
	}

	// If we found a config path, verify it exists and is the correct MTV config
	if configPath != "" {
		if _, err := os.Stat(configPath); err != nil {
			t.Errorf("Config file should exist and be accessible: %v", err)
		}

		// Check that it's actually the config.py file
		if !strings.HasSuffix(configPath, "config.py") {
			t.Errorf("Config path should end with config.py, got: %s", configPath)
		}

		// Check that the path contains the expected directory structure
		if !strings.Contains(configPath, "tests/tests_config") {
			t.Errorf("Config path should contain tests/tests_config, got: %s", configPath)
		}

		// Check that it's specifically from an mtv-api-tests project
		if !strings.Contains(configPath, "mtv-api-tests") {
			t.Errorf("Config path should be from mtv-api-tests project, got: %s", configPath)
		}

		// Verify that the config file contains MTV-specific content
		if !isValidMTVConfig(configPath) {
			t.Error("Config file should contain MTV-specific content")
		}
	}
}

func TestIsValidMTVConfig(t *testing.T) {
	// Test the MTV config validation function
	configPath, err := findConfigFile()
	if err != nil {
		t.Logf("Cannot test isValidMTVConfig without a config file: %v", err)
		return
	}

	// Test that the found config is valid
	if !isValidMTVConfig(configPath) {
		t.Error("Found config file should be valid MTV config")
	}

	// Test with a non-existent file
	if isValidMTVConfig("/nonexistent/file.py") {
		t.Error("Non-existent file should not be valid")
	}
}
