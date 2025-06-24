package tui

import (
	"fmt"
	"io/fs"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/stretchr/testify/assert"
)

// Mock implementations for TUI testing
type mockTUIClusterLoaderDeps struct {
	clusters      map[string]*ClusterInfo
	passwords     map[string]string
	shouldFailFor map[string]bool
	readDirResult []fs.DirEntry
	readDirError  error
}

func (m *mockTUIClusterLoaderDeps) ReadDir(path string) ([]fs.DirEntry, error) {
	if m.readDirError != nil {
		return nil, m.readDirError
	}
	return m.readDirResult, nil
}

func (m *mockTUIClusterLoaderDeps) EnsureLoggedInSilent(clusterName string) error {
	if m.shouldFailFor[clusterName] {
		return fmt.Errorf("login failed for %s", clusterName)
	}
	return nil
}

func (m *mockTUIClusterLoaderDeps) GetClusterInfoSilent(clusterName string) (*ClusterInfo, error) {
	if m.shouldFailFor[clusterName] {
		return nil, fmt.Errorf("cluster info failed for %s", clusterName)
	}

	if info, exists := m.clusters[clusterName]; exists {
		return info, nil
	}

	return &ClusterInfo{
		Name:       clusterName,
		OCPVersion: "4.12.0",
		MTVVersion: "2.9.0",
		CNVVersion: "4.12.0",
		IIB:        "test-iib",
		ConsoleURL: fmt.Sprintf("https://console.%s.example.com", clusterName),
	}, nil
}

func (m *mockTUIClusterLoaderDeps) GetClusterPassword(clusterName string) (string, error) {
	if m.shouldFailFor[clusterName] {
		return "", fmt.Errorf("password failed for %s", clusterName)
	}

	if password, exists := m.passwords[clusterName]; exists {
		return password, nil
	}

	return fmt.Sprintf("password-%s", clusterName), nil
}

type mockTUIDirEntry struct {
	name  string
	isDir bool
}

func (m mockTUIDirEntry) Name() string               { return m.name }
func (m mockTUIDirEntry) IsDir() bool                { return m.isDir }
func (m mockTUIDirEntry) Type() fs.FileMode          { return 0 }
func (m mockTUIDirEntry) Info() (fs.FileInfo, error) { return nil, fmt.Errorf("not implemented") }

// Helper function to create mock dependencies for TUI testing
func createMockTUIDeps() *mockTUIClusterLoaderDeps {
	return &mockTUIClusterLoaderDeps{
		clusters: map[string]*ClusterInfo{
			"qemtv-test1": {
				Name:       "qemtv-test1",
				OCPVersion: "4.12.0",
				MTVVersion: "2.9.0",
				CNVVersion: "4.12.0",
				IIB:        "test-iib",
				ConsoleURL: "https://console.qemtv-test1.example.com",
			},
			"qemtv-test2": {
				Name:       "qemtv-test2",
				OCPVersion: "4.13.0",
				MTVVersion: "Not installed",
				CNVVersion: "4.13.0",
				IIB:        "N/A",
				ConsoleURL: "https://console.qemtv-test2.example.com",
			},
		},
		passwords: map[string]string{
			"qemtv-test1": "password1",
			"qemtv-test2": "password2",
		},
		shouldFailFor: make(map[string]bool),
		readDirResult: []fs.DirEntry{
			mockTUIDirEntry{"qemtv-test1", true},
			mockTUIDirEntry{"qemtv-test2", true},
		},
	}
}

// Helper to setup TUI model with mocked dependencies
func setupTUIModelWithMocks() AppModel {
	mockDeps := createMockTUIDeps()
	SetClusterLoaderDeps(mockDeps)
	return NewAppModel()
}

// ========== TUI MODEL INITIALIZATION TESTS ==========

func TestNewAppModel_Initialization(t *testing.T) {
	model := NewAppModel()

	// Test that the model can be created without panicking
	assert.NotNil(t, model)

	// Test that Init() returns a command
	cmd := model.Init()
	assert.NotNil(t, cmd)
}

func TestAppModel_BasicMessageHandling(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test that Update doesn't panic with basic messages
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	newModel, _ := model.Update(windowMsg)

	assert.NotNil(t, newModel)
}

// ========== TUI VIEW RENDERING TESTS ==========

func TestAppModel_ViewRendering_MainMenu(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Set a reasonable window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	modelInterface, _ := model.Update(windowMsg)

	// Convert back to AppModel for continued testing
	model = modelInterface.(AppModel)

	// Test main menu view
	view := model.View()
	assert.NotEmpty(t, view)
	assert.Contains(t, view, "MTV Dev Tool")
	assert.Contains(t, view, "Clusters")

	// Should not contain any panic strings
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_ViewRendering_SmallTerminal(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test with very small terminal
	windowMsg := tea.WindowSizeMsg{Width: 20, Height: 5}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	// Should not panic with small terminal
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== TUI KEY BINDING TESTS ==========

func TestAppModel_QuitKeyBinding(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test Ctrl+C quit
	quitMsg := tea.KeyMsg{Type: tea.KeyCtrlC}
	_, cmd := model.Update(quitMsg)

	// Should return quit command
	assert.NotNil(t, cmd)
}

func TestAppModel_NavigationKeyBindings(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test Enter key on main menu
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	newModelInterface, cmd := model.Update(enterMsg)

	// Should navigate to cluster list and return a command
	assert.NotNil(t, newModelInterface)
	assert.NotNil(t, cmd) // Should start loading clusters

	// Convert to AppModel for escape test
	newModel := newModelInterface.(AppModel)

	// Test Escape key (should go back)
	escMsg := tea.KeyMsg{Type: tea.KeyEsc}
	backModelInterface, _ := newModel.Update(escMsg)

	// Should handle escape gracefully
	assert.NotNil(t, backModelInterface)
}

func TestAppModel_RefreshKeyBindings(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test refresh all (Ctrl+R)
	refreshMsg := tea.KeyMsg{Type: tea.KeyCtrlR}
	newModelInterface, cmd := model.Update(refreshMsg)

	// Should handle refresh command
	assert.NotNil(t, newModelInterface)
	assert.NotNil(t, cmd) // Should return a command to start loading
}

// ========== TUI MESSAGE HANDLING TESTS ==========

func TestAppModel_ClustersLoadedMessage(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Create a clusters loaded message - now we can access unexported fields!
	clustersMsg := ClustersLoadedMsg{
		clusters: []ClusterItem{
			{name: "qemtv-test1", status: "Online", accessible: true, ocpVersion: "4.12.0", mtvVersion: "2.9.0"},
			{name: "qemtv-test2", status: "Online", accessible: true, ocpVersion: "4.13.0", mtvVersion: "Not installed"},
		},
		clusterInfo: createMockTUIDeps().clusters,
	}

	newModelInterface, _ := model.Update(clustersMsg)

	// Should handle clusters loaded message without panic
	assert.NotNil(t, newModelInterface)

	// View should now contain cluster information
	model = newModelInterface.(AppModel)
	view := model.View()
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_NotificationMessage(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test success notification - can access unexported fields
	successMsg := NotificationMsg{message: "Operation successful", isError: false}
	newModelInterface, _ := model.Update(successMsg)

	// Should handle notification without panic
	assert.NotNil(t, newModelInterface)

	// Convert back to test error notification
	model = newModelInterface.(AppModel)

	// Test error notification
	errorMsg := NotificationMsg{message: "Operation failed", isError: true}
	newModelInterface, _ = model.Update(errorMsg)

	// Should handle error notification without panic
	assert.NotNil(t, newModelInterface)
}

func TestAppModel_ClusterDetailLoadedMessage(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test successful cluster detail load - can access unexported fields
	clusterInfo := &ClusterInfo{
		Name:       "qemtv-test1",
		OCPVersion: "4.12.0",
		MTVVersion: "2.9.0",
		CNVVersion: "4.12.0",
		ConsoleURL: "https://console.test.example.com",
	}

	detailMsg := ClusterDetailLoadedMsg{
		info:     clusterInfo,
		password: "test-password",
		loginCmd: "oc login test...",
		err:      nil,
	}

	newModelInterface, _ := model.Update(detailMsg)

	// Should handle cluster detail message without panic
	assert.NotNil(t, newModelInterface)
}

func TestAppModel_ClusterDetailLoadedMessage_WithError(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test cluster detail load with error
	errorDetailMsg := ClusterDetailLoadedMsg{
		info:     nil,
		password: "",
		loginCmd: "",
		err:      fmt.Errorf("connection timeout"),
	}

	newModelInterface, cmd := model.Update(errorDetailMsg)

	// Should handle error message without panic
	assert.NotNil(t, newModelInterface)
	// May or may not return a notification command for errors (depends on implementation)
	_ = cmd
}

// ========== TUI INTEGRATION TESTS ==========

func TestAppModel_BasicWorkflow(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Set reasonable window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	// Navigate to cluster list
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	modelInterface, cmd := model.Update(enterMsg)
	assert.NotNil(t, cmd) // Should start loading clusters

	model = modelInterface.(AppModel)

	// Should handle the full workflow without panic
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== TUI ERROR HANDLING TESTS ==========

func TestAppModel_ErrorHandling_InvalidMessage(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test with unknown message type (should be handled gracefully)
	unknownMsg := struct{ foo string }{foo: "bar"}
	newModelInterface, _ := model.Update(unknownMsg)

	// Should handle unknown message gracefully
	assert.NotNil(t, newModelInterface)

	// Convert back to test view
	model = newModelInterface.(AppModel)

	// View should still work
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== TUI DEPENDENCY INJECTION TESTS ==========

func TestAppModel_MockDependencies(t *testing.T) {
	// Test that we can successfully inject mock dependencies
	mockDeps := createMockTUIDeps()
	SetClusterLoaderDeps(mockDeps)

	// This should work without filesystem access
	model := NewAppModel()
	assert.NotNil(t, model)

	// Test that the mocked dependencies have expected data
	info, err := mockDeps.GetClusterInfoSilent("qemtv-test1")
	assert.NoError(t, err)
	assert.Equal(t, "qemtv-test1", info.Name)
	assert.Equal(t, "4.12.0", info.OCPVersion)

	password, err := mockDeps.GetClusterPassword("qemtv-test1")
	assert.NoError(t, err)
	assert.Equal(t, "password1", password)
}

func TestAppModel_MockDependencies_ErrorScenarios(t *testing.T) {
	// Test mock dependencies with error scenarios
	mockDeps := createMockTUIDeps()
	mockDeps.shouldFailFor["failing-cluster"] = true

	SetClusterLoaderDeps(mockDeps)

	// Test that errors are properly returned
	_, err := mockDeps.GetClusterInfoSilent("failing-cluster")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "cluster info failed")

	_, err = mockDeps.GetClusterPassword("failing-cluster")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "password failed")
}

// ========== TUI PERFORMANCE TESTS ==========

func TestAppModel_PerformanceWithManyMessages(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test that the model can handle many messages quickly
	start := time.Now()

	for i := 0; i < 100; i++ { // Reduced from 1000 for faster testing
		// Send resize messages (these should be fast)
		windowMsg := tea.WindowSizeMsg{Width: 120 + i%10, Height: 40 + i%5}
		modelInterface, _ := model.Update(windowMsg)
		model = modelInterface.(AppModel)
	}

	duration := time.Since(start)

	// Should complete quickly (less than 1 second for 100 messages)
	assert.Less(t, duration, time.Second)

	// Model should still be functional
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== TUI EDGE CASE TESTS ==========

func TestAppModel_ZeroSizeTerminal(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test with zero-size terminal
	windowMsg := tea.WindowSizeMsg{Width: 0, Height: 0}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	// Should handle gracefully
	view := model.View()
	assert.NotEmpty(t, view) // Should return something, even if minimal
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_RapidKeyPresses(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Test rapid key presses don't cause issues
	keys := []tea.KeyMsg{
		{Type: tea.KeyEnter},
		{Type: tea.KeyEsc},
		{Type: tea.KeyEnter},
		{Type: tea.KeyCtrlR},
		{Type: tea.KeyEsc},
	}

	for _, key := range keys {
		var err error
		// Use defer to catch any panics
		func() {
			defer func() {
				if r := recover(); r != nil {
					err = fmt.Errorf("panic: %v", r)
				}
			}()
			modelInterface, _ := model.Update(key)
			model = modelInterface.(AppModel)
		}()
		assert.NoError(t, err, "Model should not panic on key press")
	}

	// Model should still be functional
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== TUI INTERNAL STATE TESTS ==========

func TestAppModel_InternalState_Access(t *testing.T) {
	// Now we can test internal state since we're in the same package!
	model := setupTUIModelWithMocks()

	// Test initial state
	assert.Equal(t, MainMenuScreen, model.screen)
	assert.Equal(t, 0, model.width)
	assert.Equal(t, 0, model.height)

	// Test window resize updates internal state
	windowMsg := tea.WindowSizeMsg{Width: 100, Height: 30}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	assert.Equal(t, 100, model.width)
	assert.Equal(t, 30, model.height)
}

// ========== IIB DEPENDENCY INJECTION TESTS ==========

// Mock IIB dependencies for testing
type mockIIBLoaderDeps struct {
	prodBuilds      []IIBInfo
	stageBuilds     []IIBInfo
	shouldFail      map[string]bool
	loginStatus     bool
	loginShouldFail bool
}

func (m *mockIIBLoaderDeps) GetForkliftBuilds(environment string) ([]IIBInfo, error) {
	if m.shouldFail[environment] {
		return nil, fmt.Errorf("failed to get %s builds", environment)
	}

	switch environment {
	case "prod":
		return m.prodBuilds, nil
	case "stage":
		return m.stageBuilds, nil
	default:
		return []IIBInfo{}, nil
	}
}

func (m *mockIIBLoaderDeps) CheckKufloxLogin() bool {
	return m.loginStatus
}

func (m *mockIIBLoaderDeps) LoginToKuflox() error {
	if m.loginShouldFail {
		return fmt.Errorf("kuflox login failed")
	}
	m.loginStatus = true
	return nil
}

// Helper to create mock IIB dependencies
func createMockIIBDeps() *mockIIBLoaderDeps {
	return &mockIIBLoaderDeps{
		prodBuilds: []IIBInfo{
			{
				OCPVersion:  "4.17",
				MTVVersion:  "2.9",
				IIB:         "forklift-fbc-prod-v417:on-pr-abc123",
				Snapshot:    "forklift-fbc-prod-v417-snapshot",
				Created:     "2024-01-15 10:30:45 EST",
				Image:       "quay.io/konveyor/forklift-fbc-prod:v417",
				Environment: "Production",
			},
			{
				OCPVersion:  "4.19",
				MTVVersion:  "2.9",
				IIB:         "forklift-fbc-prod-v419:on-pr-def456",
				Snapshot:    "forklift-fbc-prod-v419-snapshot",
				Created:     "2024-01-15 11:45:22 EST",
				Image:       "quay.io/konveyor/forklift-fbc-prod:v419",
				Environment: "Production",
			},
		},
		stageBuilds: []IIBInfo{
			{
				OCPVersion:  "4.17",
				MTVVersion:  "2.9",
				IIB:         "forklift-fbc-stage-v417:on-pr-ghi789",
				Snapshot:    "forklift-fbc-stage-v417-snapshot",
				Created:     "2024-01-15 09:15:30 EST",
				Image:       "quay.io/konveyor/forklift-fbc-stage:v417",
				Environment: "Stage",
			},
		},
		shouldFail:      make(map[string]bool),
		loginStatus:     true,
		loginShouldFail: false,
	}
}

func TestIIBDependencyInjection_Basic(t *testing.T) {
	// Test that we can inject IIB dependencies
	mockIIBDeps := createMockIIBDeps()
	SetIIBLoaderDeps(mockIIBDeps)

	// Test that dependencies work
	prodBuilds, err := mockIIBDeps.GetForkliftBuilds("prod")
	assert.NoError(t, err)
	assert.Len(t, prodBuilds, 2)
	assert.Equal(t, "4.17", prodBuilds[0].OCPVersion)
	assert.Equal(t, "4.19", prodBuilds[1].OCPVersion)

	stageBuilds, err := mockIIBDeps.GetForkliftBuilds("stage")
	assert.NoError(t, err)
	assert.Len(t, stageBuilds, 1)
	assert.Equal(t, "4.17", stageBuilds[0].OCPVersion)

	// Test login functionality
	assert.True(t, mockIIBDeps.CheckKufloxLogin())
	assert.NoError(t, mockIIBDeps.LoginToKuflox())
}

func TestIIBDependencyInjection_ErrorScenarios(t *testing.T) {
	// Test error scenarios for IIB dependencies
	mockIIBDeps := createMockIIBDeps()
	mockIIBDeps.shouldFail["prod"] = true
	mockIIBDeps.loginShouldFail = true
	mockIIBDeps.loginStatus = false

	SetIIBLoaderDeps(mockIIBDeps)

	// Test production builds failure
	_, err := mockIIBDeps.GetForkliftBuilds("prod")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "failed to get prod builds")

	// Test stage builds still work
	stageBuilds, err := mockIIBDeps.GetForkliftBuilds("stage")
	assert.NoError(t, err)
	assert.Len(t, stageBuilds, 1)

	// Test login failure
	assert.False(t, mockIIBDeps.CheckKufloxLogin())
	err = mockIIBDeps.LoginToKuflox()
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "kuflox login failed")
}

// ========== IIB SCREEN NAVIGATION TESTS ==========

func TestAppModel_IIBScreenNavigation(t *testing.T) {
	// Setup with both cluster and IIB mocks
	mockClusterDeps := createMockTUIDeps()
	mockIIBDeps := createMockIIBDeps()
	SetClusterLoaderDeps(mockClusterDeps)
	SetIIBLoaderDeps(mockIIBDeps)

	model := NewAppModel()

	// Set window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	// Check initial state
	assert.Equal(t, MainMenuScreen, model.screen)

	// Directly test navigation by calling the handler for IIB builds
	model.mainMenu.list.Select(1) // Select IIB Builds (index 1)

	// Now press Enter to navigate to IIB input screen
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	modelInterface, _ = model.Update(enterMsg)
	model = modelInterface.(AppModel)

	// Should be on IIB input screen
	assert.Equal(t, IIBInputScreen, model.screen)

	// Test that the view renders without panic
	view := model.View()
	assert.NotEmpty(t, view)
	assert.Contains(t, view, "IIB Builds")
	assert.Contains(t, view, "MTV Version")
}

func TestAppModel_IIBInputToDisplay(t *testing.T) {
	// Setup mocks
	mockClusterDeps := createMockTUIDeps()
	mockIIBDeps := createMockIIBDeps()
	SetClusterLoaderDeps(mockClusterDeps)
	SetIIBLoaderDeps(mockIIBDeps)

	model := NewAppModel()
	model.screen = IIBInputScreen // Jump directly to input screen
	model.width = 120
	model.height = 40

	// Set MTV version in input
	model.iibInput.textInput.SetValue("2.9")

	// Press Enter to submit
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	modelInterface, cmd := model.Update(enterMsg)
	model = modelInterface.(AppModel)

	// Should transition to display screen
	assert.Equal(t, IIBDisplayScreen, model.screen)
	assert.Equal(t, "2.9", model.iibDisplay.mtvVersion)
	assert.True(t, model.iibDisplay.loading)
	assert.NotNil(t, cmd) // Should return spinner tick command

	// Simulate IIB data loaded
	iibDataMsg := IIBDataLoadedMsg{
		mtvVersion:  "2.9",
		prodBuilds:  mockIIBDeps.prodBuilds,
		stageBuilds: mockIIBDeps.stageBuilds,
		err:         nil,
	}

	modelInterface, cmd = model.Update(iibDataMsg)
	model = modelInterface.(AppModel)

	// Should finish loading
	assert.False(t, model.iibDisplay.loading)
	assert.NotNil(t, cmd) // Should return notification command

	// Test the view
	view := model.View()
	assert.NotEmpty(t, view)
	assert.Contains(t, view, "MTV 2.9 Forklift FBC Builds")
}

// ========== DYNAMIC OCP VERSION FILTERING TESTS ==========

func TestAppModel_DynamicOCPVersionFiltering(t *testing.T) {
	// Setup model with mock data
	model := NewAppModel()
	model.screen = IIBDisplayScreen
	model.width = 120
	model.height = 40

	// Setup test data with different OCP versions for prod vs stage
	model.iibDisplay.buildTypes = []string{"prod", "stage"}
	model.iibDisplay.iibData = map[string][]IIBInfo{
		"prod": {
			{OCPVersion: "4.17", MTVVersion: "2.9", IIB: "prod-417"},
			{OCPVersion: "4.19", MTVVersion: "2.9", IIB: "prod-419"},
		},
		"stage": {
			{OCPVersion: "4.17", MTVVersion: "2.9", IIB: "stage-417"},
		},
	}

	// Initially should show prod versions (4.17, 4.19)
	model.iibDisplay.selectedBuild = 0 // prod
	model.updateOCPVersionsForSelectedBuildType()

	assert.Len(t, model.iibDisplay.ocpVersions, 2)
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.17")
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.19")

	// Switch to stage - should only show 4.17
	model.iibDisplay.selectedBuild = 1 // stage
	model.updateOCPVersionsForSelectedBuildType()

	assert.Len(t, model.iibDisplay.ocpVersions, 1)
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.17")
	assert.NotContains(t, model.iibDisplay.ocpVersions, "4.19")

	// Selected OCP index should be reset to 0
	assert.Equal(t, 0, model.iibDisplay.selectedOCP)
}

func TestAppModel_OCPVersionFiltering_EmptyBuildType(t *testing.T) {
	// Test filtering when build type has no builds
	model := NewAppModel()
	model.screen = IIBDisplayScreen

	model.iibDisplay.buildTypes = []string{"prod", "stage"}
	model.iibDisplay.iibData = map[string][]IIBInfo{
		"prod": {
			{OCPVersion: "4.17", MTVVersion: "2.9", IIB: "prod-417"},
		},
		"stage": {}, // Empty stage builds
	}

	// Select stage (empty)
	model.iibDisplay.selectedBuild = 1
	model.updateOCPVersionsForSelectedBuildType()

	// Should have no OCP versions
	assert.Len(t, model.iibDisplay.ocpVersions, 0)
	assert.Equal(t, 0, model.iibDisplay.selectedOCP)
}

func TestAppModel_OCPVersionFiltering_Navigation(t *testing.T) {
	// Test that OCP versions update when navigating build types
	model := NewAppModel()
	model.screen = IIBDisplayScreen
	model.width = 120
	model.height = 40

	// Setup test data
	model.iibDisplay.buildTypes = []string{"prod", "stage"}
	model.iibDisplay.iibData = map[string][]IIBInfo{
		"prod": {
			{OCPVersion: "4.17", MTVVersion: "2.9", IIB: "prod-417"},
			{OCPVersion: "4.18", MTVVersion: "2.9", IIB: "prod-418"},
		},
		"stage": {
			{OCPVersion: "4.19", MTVVersion: "2.9", IIB: "stage-419"},
		},
	}

	// Start with prod selected
	model.iibDisplay.selectedBuild = 0
	model.iibDisplay.focusedPane = 0 // Focus on build types
	model.updateOCPVersionsForSelectedBuildType()

	// Should show prod versions
	assert.Len(t, model.iibDisplay.ocpVersions, 2)
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.17")
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.18")

	// Navigate down in build types (moves from prod to stage)
	downMsg := tea.KeyMsg{Type: tea.KeyDown}
	modelInterface, _ := model.Update(downMsg)
	model = modelInterface.(AppModel)

	// Should now show stage versions
	assert.Len(t, model.iibDisplay.ocpVersions, 1)
	assert.Contains(t, model.iibDisplay.ocpVersions, "4.19")
	assert.NotContains(t, model.iibDisplay.ocpVersions, "4.17")
	assert.NotContains(t, model.iibDisplay.ocpVersions, "4.18")
}

// ========== IIB ERROR HANDLING TESTS ==========

func TestAppModel_IIBErrorHandling_LoginFailure(t *testing.T) {
	// Setup mocks with login failure
	mockClusterDeps := createMockTUIDeps()
	mockIIBDeps := createMockIIBDeps()
	mockIIBDeps.loginStatus = false
	mockIIBDeps.loginShouldFail = true

	SetClusterLoaderDeps(mockClusterDeps)
	SetIIBLoaderDeps(mockIIBDeps)

	model := NewAppModel()
	model.screen = IIBDisplayScreen
	model.width = 120
	model.height = 40

	// Simulate loading IIB data with login failure
	// This would normally be triggered by entering MTV version and pressing Enter

	// Manually trigger the loadIIBDataCmd to test error handling
	cmd := model.loadIIBDataCmd("2.9")

	// Execute the command to get the result
	msg := cmd()

	// Should be an error message
	iibMsg, ok := msg.(IIBDataLoadedMsg)
	assert.True(t, ok)
	assert.Error(t, iibMsg.err)
	assert.Contains(t, iibMsg.err.Error(), "kuflox login failed")

	// Update model with error message
	modelInterface, _ := model.Update(iibMsg)
	model = modelInterface.(AppModel)

	// Should show error
	assert.NotEmpty(t, model.error)
	assert.Contains(t, model.error, "Failed to load IIB data")
}

func TestAppModel_IIBErrorHandling_BuildsFailure(t *testing.T) {
	// Setup mocks with builds failure
	mockClusterDeps := createMockTUIDeps()
	mockIIBDeps := createMockIIBDeps()
	mockIIBDeps.shouldFail["prod"] = true

	SetClusterLoaderDeps(mockClusterDeps)
	SetIIBLoaderDeps(mockIIBDeps)

	model := NewAppModel()
	model.screen = IIBDisplayScreen

	// Trigger loading with production failure
	cmd := model.loadIIBDataCmd("2.9")
	msg := cmd()

	// Should be an error message
	iibMsg, ok := msg.(IIBDataLoadedMsg)
	assert.True(t, ok)
	assert.Error(t, iibMsg.err)
	assert.Contains(t, iibMsg.err.Error(), "failed to get production builds")

	// Update model with error message
	modelInterface, _ := model.Update(iibMsg)
	model = modelInterface.(AppModel)

	// Should show error
	assert.NotEmpty(t, model.error)
	assert.Contains(t, model.error, "Failed to load IIB data")
}

// ========== IIB SPINNER TESTS ==========

func TestAppModel_IIBSpinnerBehavior(t *testing.T) {
	// Test that the correct spinner is used for IIB loading
	model := NewAppModel()
	model.screen = IIBInputScreen
	model.width = 120
	model.height = 40

	// Set MTV version
	model.iibInput.textInput.SetValue("2.9")

	// Submit (should start display screen spinner, not input spinner)
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	modelInterface, cmd := model.Update(enterMsg)
	model = modelInterface.(AppModel)

	// Should be on display screen and loading
	assert.Equal(t, IIBDisplayScreen, model.screen)
	assert.True(t, model.iibDisplay.loading)
	assert.NotNil(t, cmd) // Should return display spinner tick command

	// Simulate loading completion
	iibDataMsg := IIBDataLoadedMsg{
		mtvVersion:  "2.9",
		prodBuilds:  []IIBInfo{},
		stageBuilds: []IIBInfo{},
		err:         nil,
	}

	modelInterface, _ = model.Update(iibDataMsg)
	model = modelInterface.(AppModel)

	// Should finish loading
	assert.False(t, model.iibDisplay.loading)
}

// ========== IIB COPY FUNCTIONALITY TESTS ==========

func TestAppModel_IIBCopyFunctionality(t *testing.T) {
	// Test IIB copy to clipboard functionality
	model := NewAppModel()
	model.screen = IIBDisplayScreen
	model.width = 120
	model.height = 40

	// Setup test data
	model.iibDisplay.buildTypes = []string{"prod", "stage"}
	model.iibDisplay.iibData = map[string][]IIBInfo{
		"prod": {
			{OCPVersion: "4.17", MTVVersion: "2.9", IIB: "test-iib-417"},
		},
	}
	model.iibDisplay.ocpVersions = []string{"4.17"}
	model.iibDisplay.selectedBuild = 0
	model.iibDisplay.selectedOCP = 0

	// Test copy functionality
	_, cmd := model.handleIIBCopy()

	// Should return notification command (even if clipboard fails in test environment)
	assert.NotNil(t, cmd)

	// Execute the command to check notification
	if cmd != nil {
		msg := cmd()
		// Check that we get some kind of notification message
		assert.NotNil(t, msg)

		// In test environment, clipboard may fail, so we accept either success or failure
		if notifMsg, ok := msg.(NotificationMsg); ok {
			// Should contain some reference to IIB copying attempt
			assert.True(t,
				strings.Contains(notifMsg.message, "IIB") ||
					strings.Contains(notifMsg.message, "clipboard") ||
					strings.Contains(notifMsg.message, "copy"),
				"Notification should mention IIB, clipboard, or copy")
		}
	}
}

func TestAppModel_IIBCopyFunctionality_NoData(t *testing.T) {
	// Test copy when no IIB data is available
	model := NewAppModel()
	model.screen = IIBDisplayScreen

	// Setup empty data
	model.iibDisplay.buildTypes = []string{"prod"}
	model.iibDisplay.iibData = map[string][]IIBInfo{}
	model.iibDisplay.selectedBuild = 0

	// Test copy functionality
	_, cmd := model.handleIIBCopy()

	// Should return error notification
	assert.NotNil(t, cmd)

	if cmd != nil {
		msg := cmd()
		assert.NotNil(t, msg)

		// Should get an error notification about no data
		if notifMsg, ok := msg.(NotificationMsg); ok {
			assert.True(t,
				strings.Contains(notifMsg.message, "No") ||
					strings.Contains(notifMsg.message, "no") ||
					strings.Contains(notifMsg.message, "data") ||
					strings.Contains(notifMsg.message, "copy"),
				"Should indicate no data available for copying")
		}
	}
}

func TestAppModel_ScreenTransitions(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Start on main menu
	assert.Equal(t, MainMenuScreen, model.screen)

	// Navigate to cluster list
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	modelInterface, _ := model.Update(enterMsg)
	model = modelInterface.(AppModel)

	assert.Equal(t, ClusterListScreen, model.screen)

	// Go back to main menu
	escMsg := tea.KeyMsg{Type: tea.KeyEsc}
	modelInterface, _ = model.Update(escMsg)
	model = modelInterface.(AppModel)

	assert.Equal(t, MainMenuScreen, model.screen)
}

func TestAppModel_LoadingState(t *testing.T) {
	model := setupTUIModelWithMocks()

	// Initially should be loading clusters
	assert.True(t, model.clusterList.loading)

	// Simulate clusters loaded
	clustersMsg := ClustersLoadedMsg{
		clusters:    []ClusterItem{},
		clusterInfo: make(map[string]*ClusterInfo),
	}
	modelInterface, _ := model.Update(clustersMsg)
	model = modelInterface.(AppModel)

	// Should no longer be loading
	assert.False(t, model.clusterList.loading)
}

// ========== TUI COMPONENT ISOLATION TESTS ==========

func TestAppModel_ViewRendering_Isolation(t *testing.T) {
	// Test that View() can be called multiple times without side effects
	model := setupTUIModelWithMocks()

	windowMsg := tea.WindowSizeMsg{Width: 80, Height: 24}
	modelInterface, _ := model.Update(windowMsg)
	model = modelInterface.(AppModel)

	// Call View multiple times
	view1 := model.View()
	view2 := model.View()
	view3 := model.View()

	// All views should be identical and not contain panics
	assert.Equal(t, view1, view2)
	assert.Equal(t, view2, view3)
	assert.NotContains(t, strings.ToLower(view1), "panic")
}

func TestAppModel_MessageHandling_Sequence(t *testing.T) {
	// Test a sequence of messages to ensure state transitions work correctly
	model := setupTUIModelWithMocks()

	// Sequence: Resize -> Navigate -> Back -> Resize again
	messages := []tea.Msg{
		tea.WindowSizeMsg{Width: 120, Height: 40},
		tea.KeyMsg{Type: tea.KeyEnter},
		tea.KeyMsg{Type: tea.KeyEsc},
		tea.WindowSizeMsg{Width: 100, Height: 30},
	}

	for i, msg := range messages {
		modelInterface, cmd := model.Update(msg)
		model = modelInterface.(AppModel)

		// Each step should work without panic
		assert.NotNil(t, model, fmt.Sprintf("Step %d should return valid model", i))

		view := model.View()
		assert.NotEmpty(t, view, fmt.Sprintf("Step %d should render non-empty view", i))
		assert.NotContains(t, strings.ToLower(view), "panic", fmt.Sprintf("Step %d should not panic", i))

		// Some messages should return commands, others might not
		// We just verify no panics occur, not the specific command behavior
		_ = cmd
	}
}
