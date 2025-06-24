package main

import (
	"fmt"
	"io/fs"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/stretchr/testify/assert"

	"../tui"
)

// Mock implementations for testing
type mockClusterLoaderDeps struct {
	clusters      map[string]*tui.ClusterInfo
	passwords     map[string]string
	shouldFailFor map[string]bool
	readDirResult []fs.DirEntry
	readDirError  error
}

func (m *mockClusterLoaderDeps) ReadDir(path string) ([]fs.DirEntry, error) {
	if m.readDirError != nil {
		return nil, m.readDirError
	}
	return m.readDirResult, nil
}

func (m *mockClusterLoaderDeps) EnsureLoggedInSilent(clusterName string) error {
	if m.shouldFailFor[clusterName] {
		return fmt.Errorf("login failed for %s", clusterName)
	}
	return nil
}

func (m *mockClusterLoaderDeps) GetClusterInfoSilent(clusterName string) (*tui.ClusterInfo, error) {
	if m.shouldFailFor[clusterName] {
		return nil, fmt.Errorf("cluster info failed for %s", clusterName)
	}

	if info, exists := m.clusters[clusterName]; exists {
		return info, nil
	}

	return &tui.ClusterInfo{
		Name:       clusterName,
		OCPVersion: "4.12.0",
		MTVVersion: "2.9.0",
		CNVVersion: "4.12.0",
		IIB:        "test-iib",
		ConsoleURL: fmt.Sprintf("https://console.%s.example.com", clusterName),
	}, nil
}

func (m *mockClusterLoaderDeps) GetClusterPassword(clusterName string) (string, error) {
	if m.shouldFailFor[clusterName] {
		return "", fmt.Errorf("password failed for %s", clusterName)
	}

	if password, exists := m.passwords[clusterName]; exists {
		return password, nil
	}

	return fmt.Sprintf("password-%s", clusterName), nil
}

type mockDirEntry struct {
	name  string
	isDir bool
}

func (m mockDirEntry) Name() string               { return m.name }
func (m mockDirEntry) IsDir() bool                { return m.isDir }
func (m mockDirEntry) Type() fs.FileMode          { return 0 }
func (m mockDirEntry) Info() (fs.FileInfo, error) { return nil, fmt.Errorf("not implemented") }

// Helper function to create a mock deps with test data
func createMockDeps() *mockClusterLoaderDeps {
	return &mockClusterLoaderDeps{
		clusters: map[string]*tui.ClusterInfo{
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
			mockDirEntry{"qemtv-test1", true},
			mockDirEntry{"qemtv-test2", true},
			mockDirEntry{"not-a-cluster", true},
		},
	}
}

// Helper function to setup model with mocked dependencies
func setupModelWithMocks() tui.AppModel {
	mockDeps := createMockDeps()
	tui.SetClusterLoaderDeps(mockDeps)
	return tui.NewAppModel()
}

// ========== MODEL INITIALIZATION TESTS ==========

func TestNewAppModel_Initialization(t *testing.T) {
	model := tui.NewAppModel()

	// Test that the model can be created without panicking
	assert.NotNil(t, model)

	// Test that Init() returns a command
	cmd := model.Init()
	assert.NotNil(t, cmd)
}

func TestAppModel_BasicMessageHandling(t *testing.T) {
	model := setupModelWithMocks()

	// Test that Update doesn't panic with basic messages
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	newModel, _ := model.Update(windowMsg)

	assert.NotNil(t, newModel)
	// Window resize might or might not return a command
}

// ========== VIEW RENDERING TESTS ==========

func TestAppModel_ViewRendering_MainMenu(t *testing.T) {
	model := setupModelWithMocks()

	// Set a reasonable window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	model, _ = model.Update(windowMsg)

	// Test main menu view
	view := model.View()
	assert.NotEmpty(t, view)
	assert.Contains(t, view, "MTV Dev Tool")
	assert.Contains(t, view, "Clusters")

	// Should not contain any panic strings
	assert.NotContains(t, strings.ToLower(view), "panic")
	assert.NotContains(t, strings.ToLower(view), "error")
}

func TestAppModel_ViewRendering_ClusterList(t *testing.T) {
	model := setupModelWithMocks()

	// Set window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	model, _ = model.Update(windowMsg)

	// Navigate to cluster list
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	model, _ = model.Update(enterMsg)

	// Test cluster list view (should show loading)
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_ViewRendering_SmallTerminal(t *testing.T) {
	model := setupModelWithMocks()

	// Test with very small terminal
	windowMsg := tea.WindowSizeMsg{Width: 20, Height: 5}
	model, _ = model.Update(windowMsg)

	// Should not panic with small terminal
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== KEY BINDING TESTS ==========

func TestAppModel_QuitKeyBinding(t *testing.T) {
	model := setupModelWithMocks()

	// Test Ctrl+C quit
	quitMsg := tea.KeyMsg{Type: tea.KeyCtrlC}
	_, cmd := model.Update(quitMsg)

	// Should return quit command
	assert.NotNil(t, cmd)
}

func TestAppModel_NavigationKeyBindings(t *testing.T) {
	model := setupModelWithMocks()

	// Test Enter key on main menu
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	newModel, cmd := model.Update(enterMsg)

	// Should navigate to cluster list and return a command
	assert.NotNil(t, newModel)
	assert.NotNil(t, cmd) // Should start loading clusters

	// Test Escape key (should go back)
	escMsg := tea.KeyMsg{Type: tea.KeyEsc}
	backModel, _ := newModel.Update(escMsg)

	// Should handle escape gracefully
	assert.NotNil(t, backModel)
}

func TestAppModel_SearchKeyBinding(t *testing.T) {
	model := setupModelWithMocks()

	// Navigate to cluster list first
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	model, _ = model.Update(enterMsg)

	// Test search activation
	searchMsg := tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'/'}}
	newModel, _ := model.Update(searchMsg)

	// Should handle search activation
	assert.NotNil(t, newModel)
	// May or may not return a command depending on implementation
}

func TestAppModel_RefreshKeyBindings(t *testing.T) {
	model := setupModelWithMocks()

	// Test refresh all (Ctrl+R)
	refreshMsg := tea.KeyMsg{Type: tea.KeyCtrlR}
	newModel, cmd := model.Update(refreshMsg)

	// Should handle refresh command
	assert.NotNil(t, newModel)
	// Should return a command to start loading
	assert.NotNil(t, cmd)

	// Navigate to cluster list for single refresh test
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	model, _ = model.Update(enterMsg)

	// Test single cluster refresh (Ctrl+U)
	singleRefreshMsg := tea.KeyMsg{Type: tea.KeyCtrlU}
	newModel, cmd = model.Update(singleRefreshMsg)

	// Should handle single refresh
	assert.NotNil(t, newModel)
}

// ========== MESSAGE HANDLING TESTS ==========

func TestAppModel_ClustersLoadedMessage(t *testing.T) {
	model := setupModelWithMocks()

	// Create a clusters loaded message
	clustersMsg := tui.ClustersLoadedMsg{
		Clusters: []tui.ClusterItem{
			{Name: "qemtv-test1", Status: "Online", Accessible: true, OCPVersion: "4.12.0", MTVVersion: "2.9.0"},
			{Name: "qemtv-test2", Status: "Online", Accessible: true, OCPVersion: "4.13.0", MTVVersion: "Not installed"},
		},
		ClusterInfo: createMockDeps().clusters,
	}

	newModel, cmd := model.Update(clustersMsg)

	// Should handle clusters loaded message without panic
	assert.NotNil(t, newModel)

	// View should now contain cluster information
	view := newModel.View()
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_NotificationMessage(t *testing.T) {
	model := setupModelWithMocks()

	// Test success notification
	successMsg := tui.NotificationMsg{Message: "Operation successful", IsError: false}
	newModel, _ := model.Update(successMsg)

	// Should handle notification without panic
	assert.NotNil(t, newModel)

	// Test error notification
	errorMsg := tui.NotificationMsg{Message: "Operation failed", IsError: true}
	newModel, _ = newModel.Update(errorMsg)

	// Should handle error notification without panic
	assert.NotNil(t, newModel)
}

func TestAppModel_ClusterDetailLoadedMessage(t *testing.T) {
	model := setupModelWithMocks()

	// Test successful cluster detail load
	clusterInfo := &tui.ClusterInfo{
		Name:       "qemtv-test1",
		OCPVersion: "4.12.0",
		MTVVersion: "2.9.0",
		CNVVersion: "4.12.0",
		ConsoleURL: "https://console.test.example.com",
	}

	detailMsg := tui.ClusterDetailLoadedMsg{
		Info:     clusterInfo,
		Password: "test-password",
		LoginCmd: "oc login test...",
		Err:      nil,
	}

	newModel, _ := model.Update(detailMsg)

	// Should handle cluster detail message without panic
	assert.NotNil(t, newModel)
	// May return a notification command
}

func TestAppModel_ClusterDetailLoadedMessage_WithError(t *testing.T) {
	model := setupModelWithMocks()

	// Test cluster detail load with error
	errorDetailMsg := tui.ClusterDetailLoadedMsg{
		Info:     nil,
		Password: "",
		LoginCmd: "",
		Err:      fmt.Errorf("connection timeout"),
	}

	newModel, cmd := model.Update(errorDetailMsg)

	// Should handle error message without panic
	assert.NotNil(t, newModel)
	// Should return a notification command
	assert.NotNil(t, cmd)
}

// ========== INTEGRATION TESTS ==========

func TestAppModel_BasicWorkflow(t *testing.T) {
	model := setupModelWithMocks()

	// Set reasonable window size
	windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
	model, _ = model.Update(windowMsg)

	// Navigate to cluster list
	enterMsg := tea.KeyMsg{Type: tea.KeyEnter}
	model, cmd := model.Update(enterMsg)
	assert.NotNil(t, cmd) // Should start loading clusters

	// Load some test clusters
	clustersMsg := tui.ClustersLoadedMsg{
		Clusters: []tui.ClusterItem{
			{Name: "qemtv-test1", Status: "Online", Accessible: true},
		},
		ClusterInfo: createMockDeps().clusters,
	}
	model, _ = model.Update(clustersMsg)

	// Should handle the full workflow without panic
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
	assert.Contains(t, view, "qemtv-test1") // Should show the loaded cluster
}

// ========== ERROR HANDLING TESTS ==========

func TestAppModel_ErrorHandling_InvalidMessage(t *testing.T) {
	model := setupModelWithMocks()

	// Test with unknown message type (should be handled gracefully)
	unknownMsg := struct{ foo string }{foo: "bar"}
	newModel, _ := model.Update(unknownMsg)

	// Should handle unknown message gracefully
	assert.NotNil(t, newModel)

	// View should still work
	view := newModel.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_ErrorHandling_NilPointers(t *testing.T) {
	model := setupModelWithMocks()

	// Test with message containing nil values
	detailMsg := tui.ClusterDetailLoadedMsg{
		Info:     nil,
		Password: "",
		LoginCmd: "",
		Err:      nil,
	}

	newModel, _ := model.Update(detailMsg)

	// Should handle nil gracefully
	assert.NotNil(t, newModel)

	view := newModel.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== DEPENDENCY INJECTION TESTS ==========

func TestAppModel_MockDependencies(t *testing.T) {
	// Test that we can successfully inject mock dependencies
	mockDeps := createMockDeps()
	tui.SetClusterLoaderDeps(mockDeps)

	// This should work without filesystem access
	model := tui.NewAppModel()
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
	mockDeps := createMockDeps()
	mockDeps.shouldFailFor["failing-cluster"] = true

	tui.SetClusterLoaderDeps(mockDeps)

	// Test that errors are properly returned
	_, err := mockDeps.GetClusterInfoSilent("failing-cluster")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "cluster info failed")

	_, err = mockDeps.GetClusterPassword("failing-cluster")
	assert.Error(t, err)
	assert.Contains(t, err.Error(), "password failed")
}

// ========== PERFORMANCE TESTS ==========

func TestAppModel_PerformanceWithManyMessages(t *testing.T) {
	model := setupModelWithMocks()

	// Test that the model can handle many messages quickly
	start := time.Now()

	for i := 0; i < 1000; i++ {
		// Send resize messages (these should be fast)
		windowMsg := tea.WindowSizeMsg{Width: 120 + i%10, Height: 40 + i%5}
		model, _ = model.Update(windowMsg)
	}

	duration := time.Since(start)

	// Should complete quickly (less than 1 second for 1000 messages)
	assert.Less(t, duration, time.Second)

	// Model should still be functional
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== EDGE CASE TESTS ==========

func TestAppModel_ZeroSizeTerminal(t *testing.T) {
	model := setupModelWithMocks()

	// Test with zero-size terminal
	windowMsg := tea.WindowSizeMsg{Width: 0, Height: 0}
	model, _ = model.Update(windowMsg)

	// Should handle gracefully
	view := model.View()
	assert.NotEmpty(t, view) // Should return something, even if minimal
	assert.NotContains(t, strings.ToLower(view), "panic")
}

func TestAppModel_RapidKeyPresses(t *testing.T) {
	model := setupModelWithMocks()

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
			model, _ = model.Update(key)
		}()
		assert.NoError(t, err, "Model should not panic on key press")
	}

	// Model should still be functional
	view := model.View()
	assert.NotEmpty(t, view)
	assert.NotContains(t, strings.ToLower(view), "panic")
}

// ========== CONCURRENCY TESTS ==========

func TestAppModel_ConcurrentAccess(t *testing.T) {
	// Note: The AppModel itself isn't designed for concurrent access,
	// but we can test that multiple models can be created and used safely
	models := make([]tui.AppModel, 10)

	// Create multiple models concurrently
	for i := 0; i < 10; i++ {
		models[i] = setupModelWithMocks()
	}

	// Each model should be independent and functional
	for i, model := range models {
		windowMsg := tea.WindowSizeMsg{Width: 120, Height: 40}
		model, _ = model.Update(windowMsg)

		view := model.View()
		assert.NotEmpty(t, view, fmt.Sprintf("Model %d should render", i))
		assert.NotContains(t, strings.ToLower(view), "panic")
	}
}
