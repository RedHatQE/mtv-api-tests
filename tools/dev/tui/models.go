package tui

import (
	"fmt"
	"io"
	"io/fs"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/atotto/clipboard"
	"github.com/charmbracelet/bubbles/help"
	"github.com/charmbracelet/bubbles/key"
	"github.com/charmbracelet/bubbles/list"
	"github.com/charmbracelet/bubbles/progress"
	"github.com/charmbracelet/bubbles/spinner"
	"github.com/charmbracelet/bubbles/table"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// Constants from main package
const CLUSTERS_PATH = "/mnt/cnv-qe.rhcloud.com"

// Key bindings for help system
type keyMap struct {
	Up            key.Binding
	Down          key.Binding
	Enter         key.Binding
	Search        key.Binding
	Refresh       key.Binding
	RefreshSingle key.Binding // Single cluster refresh
	Back          key.Binding
	Quit          key.Binding
}

var keys = keyMap{
	Up: key.NewBinding(
		key.WithKeys("up", "k"),
		key.WithHelp("↑/k", "move up"),
	),
	Down: key.NewBinding(
		key.WithKeys("down", "j"),
		key.WithHelp("↓/j", "move down"),
	),
	Enter: key.NewBinding(
		key.WithKeys("enter"),
		key.WithHelp("enter", "select"),
	),
	Search: key.NewBinding(
		key.WithKeys("/"),
		key.WithHelp("/", "search"),
	),
	Refresh: key.NewBinding(
		key.WithKeys("ctrl+r"),
		key.WithHelp("ctrl+r", "refresh"),
	),
	RefreshSingle: key.NewBinding(
		key.WithKeys("ctrl+u"),
		key.WithHelp("ctrl+u", "refresh single cluster"),
	),
	Back: key.NewBinding(
		key.WithKeys("esc"),
		key.WithHelp("esc", "back"),
	),
	Quit: key.NewBinding(
		key.WithKeys("q", "ctrl+c"),
		key.WithHelp("q", "quit"),
	),
}

// ShortHelp returns keybindings to be shown in the mini help view
func (k keyMap) ShortHelp() []key.Binding {
	return []key.Binding{k.Enter, k.Search, k.Refresh, k.RefreshSingle, k.Back, k.Quit}
}

// FullHelp returns keybindings for the expanded help view
func (k keyMap) FullHelp() [][]key.Binding {
	return [][]key.Binding{
		{k.Up, k.Down, k.Enter},
		{k.Search, k.Refresh, k.RefreshSingle, k.Back, k.Quit},
	}
}

// These are imported from the main package
// We need to access the helper functions for cluster operations
type ClusterInfo struct {
	Name       string
	OCPVersion string
	MTVVersion string
	CNVVersion string
	IIB        string
	ConsoleURL string
}

// Helper function interfaces to access main package functionality
type ClusterLoaderDeps interface {
	ReadDir(path string) ([]fs.DirEntry, error)
	EnsureLoggedInSilent(clusterName string) error
	GetClusterInfoSilent(clusterName string) (*ClusterInfo, error)
	GetClusterPassword(clusterName string) (string, error)
}

// Default implementation that calls the main package functions
type defaultClusterLoaderDeps struct{}

func (d *defaultClusterLoaderDeps) ReadDir(path string) ([]fs.DirEntry, error) {
	return os.ReadDir(path)
}

func (d *defaultClusterLoaderDeps) EnsureLoggedInSilent(clusterName string) error {
	// This will be injected from main package - silent version
	return fmt.Errorf("ensureLoggedInSilent not available in TUI context")
}

func (d *defaultClusterLoaderDeps) GetClusterInfoSilent(clusterName string) (*ClusterInfo, error) {
	// This will be injected from main package - silent version
	return nil, fmt.Errorf("getClusterInfoSilent not available in TUI context")
}

func (d *defaultClusterLoaderDeps) GetClusterPassword(clusterName string) (string, error) {
	// This will be injected from main package
	return "", fmt.Errorf("getClusterPassword not available in TUI context")
}

// Global dependency injection
var clusterLoaderDeps ClusterLoaderDeps = &defaultClusterLoaderDeps{}

// SetClusterLoaderDeps allows injecting dependencies from main package
func SetClusterLoaderDeps(deps ClusterLoaderDeps) {
	clusterLoaderDeps = deps
}

// Screen types
type ScreenType int

const (
	MainMenuScreen ScreenType = iota
	ClusterListScreen
	ClusterDetailScreen
	TestConfigScreen
	ProgressScreen
	ResultsScreen
)

// Application state
type AppModel struct {
	screen            ScreenType
	previousScreen    ScreenType // Track navigation history
	selectedCluster   string
	mainMenu          MainMenuModel
	clusterList       ClusterListModel
	clusterDetail     ClusterDetailModel
	error             string
	notification      string    // For non-error notifications like copy success
	notificationTimer time.Time // When notification expires
	width             int
	height            int
	help              help.Model
	keys              keyMap
}

// Main menu item
type MainMenuItem struct {
	title       string
	description string
	action      string
}

func (i MainMenuItem) FilterValue() string { return i.title }
func (i MainMenuItem) Title() string       { return i.title }
func (i MainMenuItem) Description() string { return i.description }

// Main menu model
type MainMenuModel struct {
	list list.Model
}

// Cluster item for the list
type ClusterItem struct {
	name       string
	status     string
	ocpVersion string
	mtvVersion string
	cnvVersion string
	accessible bool
}

func (i ClusterItem) FilterValue() string {
	// Make multiple fields searchable: name, status, versions
	searchText := i.name + " " + i.status + " " + i.ocpVersion + " " + i.mtvVersion + " " + i.cnvVersion
	if i.accessible {
		searchText += " online accessible"
	} else {
		searchText += " offline inaccessible"
	}
	return searchText
}
func (i ClusterItem) Title() string { return i.name }
func (i ClusterItem) Description() string {
	status := "❌ Offline"
	if i.accessible {
		status = fmt.Sprintf("✅ OCP %s, MTV %s", i.ocpVersion, i.mtvVersion)
	}
	return status
}

// Cluster list model for multi-pane layout
type ClusterListModel struct {
	list             list.Model
	loading          bool
	spinner          spinner.Model
	clusters         []ClusterItem
	clusterInfo      map[string]*ClusterInfo // Cache for full cluster info
	clusterPasswords map[string]string       // Cache for cluster passwords
	table            table.Model             // Left pane: cluster table
	progress         progress.Model          // Add progress bar for loading
	searchInput      textinput.Model         // Search input field
	searching        bool                    // Whether in search mode
	filteredRows     []table.Row             // Filtered table rows for search
	selectedIndex    int                     // Currently selected cluster index
	detailView       ClusterDetailModel      // Right pane: cluster details
	focusedPane      int                     // 0 = left pane, 1 = right pane
}

// Cluster operations menu item - REMOVE THIS TYPE
// type ClusterOpsMenuItem struct {
// 	title       string
// 	description string
// 	action      string
// }

// func (i ClusterOpsMenuItem) FilterValue() string { return i.title }
// func (i ClusterOpsMenuItem) Title() string       { return i.title }
// func (i ClusterOpsMenuItem) Description() string { return i.description }

// Cluster operations model - REMOVE THIS TYPE
// type ClusterOperationsModel struct {
// 	list     list.Model
// 	selected int
// }

// Cluster detail model
type ClusterDetailModel struct {
	info     *ClusterInfo
	password string
	loginCmd string
	loading  bool
	updating bool // Flag to indicate single cluster refresh in progress
	spinner  spinner.Model
	table    table.Model
}

// Messages for async operations
type ClustersLoadedMsg struct {
	clusters    []ClusterItem
	clusterInfo map[string]*ClusterInfo
}
type ClusterStatusMsg struct{}
type ClusterLoadingProgressMsg struct{}

// Progress tracking messages
type ClusterLoadingStartedMsg struct{}

type ClusterLoadedMsg struct{}

// New messages for cluster operations
type ClusterPasswordLoadedMsg struct {
	clusterName string
	password    string
	err         error
}

type ClusterDetailLoadedMsg struct {
	info     *ClusterInfo
	password string
	loginCmd string
	err      error
}

// Clipboard helper function
func clipboardWriteAll(text string) error {
	return clipboard.WriteAll(text)
}

// Notification message for auto-clearing notifications
type NotificationMsg struct {
	message string
	isError bool
}

// Timer message for clearing notifications
type NotificationClearMsg struct{}

// Helper function to show notification with auto-clear timer
func showNotification(message string, isError bool) tea.Cmd {
	return tea.Batch(
		func() tea.Msg {
			return NotificationMsg{message: message, isError: isError}
		},
		tea.Tick(3*time.Second, func(t time.Time) tea.Msg {
			return NotificationClearMsg{}
		}),
	)
}

// Initialize the app
func NewAppModel() AppModel {
	// Setup main menu items
	items := []list.Item{
		MainMenuItem{
			title:       "📋 Clusters",
			description: "Browse available clusters (press 'q' to quit)",
			action:      "list-clusters",
		},
	}

	// Create main menu list
	mainMenuList := list.New(items, MainMenuDelegate{}, 50, 14)
	mainMenuList.Title = "MTV Dev Tool"
	mainMenuList.SetShowStatusBar(false)
	mainMenuList.SetFilteringEnabled(false)
	mainMenuList.Styles.Title = titleStyle

	// Create cluster list with manual filtering
	clusterList := list.New([]list.Item{}, ClusterDelegate{}, 80, 20)
	clusterList.Title = "Available Clusters"
	clusterList.SetShowStatusBar(true)
	clusterList.SetFilteringEnabled(false) // Disable automatic filtering
	clusterList.Styles.Title = titleStyle

	// Create cluster table
	clusterTableColumns := []table.Column{
		{Title: "Cluster", Width: 20},
		{Title: "Status", Width: 15},
	}

	clusterTable := table.New(
		table.WithColumns(clusterTableColumns),
		table.WithRows([]table.Row{}),
		table.WithFocused(true),
	)

	// Style the cluster table
	tableStyles := table.DefaultStyles()
	tableStyles.Header = tableStyles.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		BorderBottom(true).
		Bold(true)
	tableStyles.Selected = tableStyles.Selected.
		Foreground(lipgloss.Color("229")).
		Background(lipgloss.Color("57")).
		Bold(false)
	clusterTable.SetStyles(tableStyles)

	// Setup spinner for loading
	s := spinner.New()
	s.Spinner = spinner.Dot
	s.Style = spinnerStyle

	// Setup progress bar for cluster loading
	prog := progress.New(progress.WithDefaultGradient())

	// Setup help model
	h := help.New()

	// Setup search input
	ti := textinput.New()
	ti.Placeholder = "Search clusters..."
	ti.CharLimit = 50
	ti.Width = 30

	// Detail spinner for right pane
	detailSpinner := spinner.New()
	detailSpinner.Spinner = spinner.Dot
	detailSpinner.Style = spinnerStyle

	return AppModel{
		screen: MainMenuScreen,
		mainMenu: MainMenuModel{
			list: mainMenuList,
		},
		clusterList: ClusterListModel{
			list:             clusterList,
			spinner:          s,
			loading:          true,                          // Start loading clusters immediately
			clusterInfo:      make(map[string]*ClusterInfo), // Initialize cache
			clusterPasswords: make(map[string]string),       // Initialize password cache
			table:            clusterTable,                  // Left pane: cluster table
			progress:         prog,                          // Add progress component
			searchInput:      ti,                            // Add search input
			selectedIndex:    0,                             // Start with first cluster selected
			detailView: ClusterDetailModel{
				spinner: detailSpinner,
				loading: false, // Will load when cluster is selected
			},
		},
		clusterDetail: ClusterDetailModel{
			spinner: detailSpinner,
		},
		help: h,
		keys: keys,
	}
}

// Init initializes the model (required by tea.Model interface)
func (m AppModel) Init() tea.Cmd {
	// Start both spinner and background cluster loading
	return tea.Batch(
		m.clusterList.spinner.Tick,
		m.loadClustersCmd(),
	)
}

// Update handles messages and state changes
func (m AppModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmd tea.Cmd

	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height

		// Calculate available space for content (leave room for header and footer)
		contentHeight := m.height - 8 // Reserve space for header, footer, and margins
		contentWidth := m.width - 4   // Use full terminal width with reasonable margins

		// Update list dimensions for all screens
		m.mainMenu.list.SetWidth(contentWidth)
		m.mainMenu.list.SetHeight(contentHeight)
		m.clusterList.list.SetWidth(contentWidth)
		m.clusterList.list.SetHeight(contentHeight)

		// Update help system width
		m.help.Width = m.width

		// Force recalculation of table dimensions for cluster list
		if m.screen == ClusterListScreen && len(m.clusterList.table.Rows()) > 0 {
			// Recalculate table dimensions based on new terminal size
			totalWidth := m.width - 4
			leftWidth := totalWidth * 3 / 10     // ~30% for cluster table (smaller since only name + status)
			rightWidth := totalWidth - leftWidth // ~70% for details (more space for detailed info)

			// Update left table columns
			if leftWidth > 40 { // Only if we have reasonable space
				availableTableWidth := leftWidth - 6
				tableColumns := []table.Column{
					{Title: "Cluster", Width: availableTableWidth * 6 / 10}, // 60% for cluster names
					{Title: "Status", Width: availableTableWidth * 4 / 10},  // 40% for status
				}
				m.clusterList.table.SetColumns(tableColumns)
			}

			// Update right table if it exists
			if m.clusterList.detailView.info != nil {
				m.setupRightPaneTable(rightWidth - 6)
			}
		}

	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "ctrl+r":
			// Refresh cluster list - works on any screen
			if m.screen == ClusterListScreen || m.screen == MainMenuScreen {
				return m.refreshClusterList()
			}
		case "ctrl+u":
			// Single cluster refresh - only works on cluster list screen
			if m.screen == ClusterListScreen && !m.clusterList.loading && !m.clusterList.searching {
				return m.refreshSingleCluster()
			}
		case "/":
			// Activate search - only works on cluster list screen
			if m.screen == ClusterListScreen && !m.clusterList.loading {
				m.clusterList.searching = true
				m.clusterList.searchInput.Focus()
				return m, textinput.Blink
			}
		case "esc":
			// Improved navigation - go back to previous screen
			switch m.screen {
			case ClusterListScreen:
				if m.clusterList.searching {
					// Exit search mode
					m.clusterList.searching = false
					m.clusterList.searchInput.Blur()
					m.clusterList.searchInput.SetValue("")
					// Reset table to show all clusters
					m.clusterList.table.SetRows(m.clusterList.filteredRows)
					return m, nil
				}
				m.screen = MainMenuScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				return m, nil
			case ClusterDetailScreen:
				// Go back to previous screen (should be ClusterListScreen)
				m.screen = m.previousScreen
				if m.previousScreen == MainMenuScreen {
					m.previousScreen = MainMenuScreen
				} else {
					m.previousScreen = MainMenuScreen
				}
				m.error = ""
				return m, nil
			}
		case "tab":
			// Switch between panes in cluster list screen
			if m.screen == ClusterListScreen && !m.clusterList.loading && !m.clusterList.searching {
				m.clusterList.focusedPane = 1 - m.clusterList.focusedPane // Toggle between 0 and 1
				return m, nil
			}
		case "shift+tab":
			// Switch between panes in cluster list screen (reverse direction)
			if m.screen == ClusterListScreen && !m.clusterList.loading && !m.clusterList.searching {
				m.clusterList.focusedPane = 1 - m.clusterList.focusedPane // Toggle between 0 and 1
				return m, nil
			}
		case "enter":
			switch m.screen {
			case MainMenuScreen:
				return m.handleMainMenuSelection()
			case ClusterListScreen:
				if m.clusterList.focusedPane == 1 {
					// Right pane: copy selected field to clipboard
					return m.handleRightPaneCopy()
				}
				// Left pane: do nothing - no need to go to detail screen
				return m, nil
			case ClusterDetailScreen:
				return m.handleClusterDetailTableCopy()
			}
		}

	case ClustersLoadedMsg:
		m.clusterList.loading = false
		m.clusterList.clusters = msg.clusters       // Store clusters for selection
		m.clusterList.clusterInfo = msg.clusterInfo // Store cached cluster info
		items := make([]list.Item, len(msg.clusters))
		tableRows := make([]table.Row, len(msg.clusters))

		for i, cluster := range msg.clusters {
			items[i] = cluster

			// Create table row
			statusDisplay := "❌ Offline"
			if cluster.accessible {
				// All accessible clusters show as Online regardless of MTV status
				statusDisplay = "✅ Online"
			} else {
				if cluster.status == "Timeout" {
					statusDisplay = "⏰ Timeout"
				}
			}

			tableRows[i] = table.Row{cluster.name, statusDisplay}
		}

		m.clusterList.list.SetItems(items)
		m.clusterList.table.SetRows(tableRows)
		m.clusterList.filteredRows = tableRows // Store for search filtering

		// Auto-select the first cluster to show details immediately
		if len(msg.clusters) > 0 {
			// Set cursor to first cluster and trigger detail loading for right pane
			m.clusterList.table.SetCursor(0)
			// Always trigger detail loading when clusters are loaded
			return m, m.updateSelectedClusterDetails()
		}

		return m, nil

	case ClusterPasswordLoadedMsg:
		if msg.err != nil {
			m.error = fmt.Sprintf("Failed to get password: %v", msg.err)
		} else {
			// Cache the password for future use
			m.clusterList.clusterPasswords[msg.clusterName] = msg.password

			// Update the detail view in multi-pane mode
			m.clusterList.detailView.password = msg.password
			// Generate login command if we have the info
			if m.clusterList.detailView.info != nil {
				apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", m.clusterList.detailView.info.Name)
				m.clusterList.detailView.loginCmd = fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, msg.password)
			}
			// Clear table so it gets recreated with password info
			m.clusterList.detailView.table = table.Model{}
			// Force table recreation on next render with proper width
			rightWidth := (m.width - 4) * 7 / 10 // Calculate 70% of available width
			if rightWidth < 40 {
				rightWidth = 40 // Minimum width for readability
			}
			m.setupRightPaneTable(rightWidth)
		}
		return m, nil

	case ClusterDetailLoadedMsg:
		if m.screen == ClusterDetailScreen {
			// Update standalone cluster detail screen
			m.clusterDetail.loading = false
			if msg.err != nil {
				m.error = fmt.Sprintf("Failed to load cluster details: %v", msg.err)
			} else {
				m.clusterDetail.info = msg.info
				m.clusterDetail.password = msg.password
				m.clusterDetail.loginCmd = msg.loginCmd
				m.clusterDetail.updating = false // Clear updating flag

				// Cache the password for future use
				if msg.password != "" {
					m.clusterList.clusterPasswords[msg.info.Name] = msg.password
				}

				// Setup the detail table with the loaded info
				m.setupClusterDetailTable()
			}
		} else {
			// Update the detail view in multi-pane mode
			m.clusterList.detailView.loading = false

			// Check if this was a single cluster refresh (by looking for Loading status)
			isRefresh := false
			if msg.info != nil {
				for _, cluster := range m.clusterList.clusters {
					if cluster.name == msg.info.Name && cluster.status == "Loading" {
						isRefresh = true
						break
					}
				}
			}

			if msg.err != nil {
				if isRefresh {
					// For refresh errors, we need to find the cluster name from the loading state
					var clusterName string
					for _, cluster := range m.clusterList.clusters {
						if cluster.status == "Loading" {
							clusterName = cluster.name
							break
						}
					}
					if clusterName == "" {
						clusterName = "unknown cluster"
					}
					return m, showNotification(fmt.Sprintf("Failed to refresh %s: %v", clusterName, msg.err), true)
				} else {
					m.error = fmt.Sprintf("Failed to load cluster details: %v", msg.err)
				}
			} else {
				m.clusterList.detailView.info = msg.info
				m.clusterList.detailView.password = msg.password
				m.clusterList.detailView.loginCmd = msg.loginCmd
				m.clusterList.detailView.updating = false // Clear updating flag

				// Cache the password for future use
				if msg.password != "" {
					m.clusterList.clusterPasswords[msg.info.Name] = msg.password
				}

				// Update cluster info cache
				m.clusterList.clusterInfo[msg.info.Name] = msg.info

				// Update the cluster in the clusters list and table rows
				for i, cluster := range m.clusterList.clusters {
					if cluster.name == msg.info.Name {
						// Update cluster item with fresh info
						m.clusterList.clusters[i].ocpVersion = msg.info.OCPVersion
						m.clusterList.clusters[i].mtvVersion = msg.info.MTVVersion
						m.clusterList.clusters[i].cnvVersion = msg.info.CNVVersion

						// Set status as Online for all accessible clusters
						m.clusterList.clusters[i].status = "Online"
						break
					}
				}

				// Rebuild table rows to reflect updated cluster info
				m.updateClusterTableRows()

				// Update the right pane table if empty
				if len(m.clusterList.detailView.table.Rows()) == 0 {
					rightWidth := (m.width - 4) * 7 / 10 // Calculate 70% of available width
					if rightWidth < 40 {
						rightWidth = 40 // Minimum width for readability
					}
					m.setupRightPaneTable(rightWidth)
				}

				// If this was an update operation, recreate the table with fresh data
				if isRefresh {
					rightWidth := (m.width - 4) * 7 / 10 // Calculate 70% of available width
					if rightWidth < 40 {
						rightWidth = 40 // Minimum width for readability
					}
					m.setupRightPaneTable(rightWidth - 6)
				}

				// Show completion notification for refresh
				if isRefresh {
					return m, showNotification(fmt.Sprintf("✅ %s refreshed successfully", msg.info.Name), false)
				}
			}
		}
		return m, nil

	case ClusterLoadingStartedMsg:
		// Just acknowledge the start, no progress tracking needed
		return m, nil

	case ClusterLoadedMsg:
		// Individual cluster loaded - no action needed since we load async
		return m, nil

	case NotificationMsg:
		// Handle notification messages
		if msg.isError {
			m.error = msg.message
			m.notification = ""
		} else {
			m.notification = msg.message
			m.error = ""
		}
		m.notificationTimer = time.Now().Add(3 * time.Second)
		return m, nil

	case NotificationClearMsg:
		// Clear notification if timer has expired
		if time.Now().After(m.notificationTimer) {
			m.notification = ""
		}
		return m, nil

	case ClusterSelectionChangedMsg:
		// Handle cluster selection change in multi-pane mode
		m.selectedCluster = msg.clusterName

		if !msg.cluster.accessible {
			// Clear detail view for inaccessible clusters
			m.clusterList.detailView.info = nil
			m.clusterList.detailView.password = ""
			m.clusterList.detailView.loginCmd = ""
			m.clusterList.detailView.loading = false
			m.clusterList.detailView.table = table.Model{} // Clear table
			return m, nil
		}

		// Check if cluster info is already cached
		if cachedInfo, exists := m.clusterList.clusterInfo[msg.cluster.name]; exists {
			// Use cached info immediately - no loading needed
			m.clusterList.detailView.loading = false
			m.clusterList.detailView.info = cachedInfo

			// Check if password is also cached
			if cachedPassword, passwordExists := m.clusterList.clusterPasswords[msg.cluster.name]; passwordExists {
				// Use cached password and generate login command immediately
				m.clusterList.detailView.password = cachedPassword
				apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", cachedInfo.Name)
				m.clusterList.detailView.loginCmd = fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, cachedPassword)

				// Clear table so it gets recreated with cached data
				m.clusterList.detailView.table = table.Model{}

				// Force table recreation with proper width
				rightWidth := (m.width - 4) * 7 / 10 // Calculate 70% of available width
				if rightWidth < 40 {
					rightWidth = 40 // Minimum width for readability
				}
				m.setupRightPaneTable(rightWidth)

				return m, nil // No need to load anything
			} else {
				// Info cached but password not cached - load password only
				m.clusterList.detailView.password = "" // Reset until loaded
				m.clusterList.detailView.loginCmd = "" // Reset until password loaded

				// Clear table so it gets recreated with new data
				m.clusterList.detailView.table = table.Model{}

				return m, m.loadClusterPasswordCmd(msg.cluster.name)
			}
		}

		// Start loading cluster details (both info and password)
		m.clusterList.detailView.loading = true
		m.clusterList.detailView.info = nil
		m.clusterList.detailView.password = ""
		m.clusterList.detailView.loginCmd = ""
		m.clusterList.detailView.table = table.Model{} // Clear table
		return m, tea.Batch(m.clusterList.detailView.spinner.Tick, m.loadClusterDetailCmd(msg.cluster.name, "cluster-info"))
	}

	// Handle screen-specific updates
	switch m.screen {
	case MainMenuScreen:
		m.mainMenu.list, cmd = m.mainMenu.list.Update(msg)
		// Also update spinner if clusters are loading in background
		if m.clusterList.loading {
			var spinnerCmd tea.Cmd
			m.clusterList.spinner, spinnerCmd = m.clusterList.spinner.Update(msg)
			if cmd != nil && spinnerCmd != nil {
				cmd = tea.Batch(cmd, spinnerCmd)
			} else if spinnerCmd != nil {
				cmd = spinnerCmd
			}
		}
	case ClusterDetailScreen:
		if !m.clusterDetail.loading {
			m.clusterDetail.table, cmd = m.clusterDetail.table.Update(msg)
		} else {
			m.clusterDetail.spinner, cmd = m.clusterDetail.spinner.Update(msg)
		}
	case ClusterListScreen:
		if m.clusterList.loading {
			m.clusterList.spinner, cmd = m.clusterList.spinner.Update(msg)
		} else if m.clusterList.searching {
			// Handle both search input and table navigation in search mode
			var searchCmd tea.Cmd
			m.clusterList.searchInput, searchCmd = m.clusterList.searchInput.Update(msg)

			// Filter table rows based on search input
			query := m.clusterList.searchInput.Value()
			filteredRows := m.filterClusters(query)
			m.clusterList.table.SetRows(filteredRows)

			// Also allow table navigation (but prioritize search input for typing)
			if msg, ok := msg.(tea.KeyMsg); ok {
				switch msg.String() {
				case "up", "down":
					// Let table handle navigation keys and check for selection change
					oldCursor := m.clusterList.table.Cursor()
					m.clusterList.table, cmd = m.clusterList.table.Update(msg)
					if m.clusterList.table.Cursor() != oldCursor {
						newCmd := m.updateSelectedClusterDetails()
						if newCmd != nil {
							cmd = tea.Batch(cmd, newCmd)
						}
					}
				case "enter":
					// Handle enter in the main switch
				default:
					// Search input already handled above
					cmd = searchCmd
				}
			} else {
				cmd = searchCmd
			}
		} else {
			// Handle navigation based on focused pane
			if m.clusterList.focusedPane == 0 {
				// Left pane: Update cluster table
				oldCursor := m.clusterList.table.Cursor()
				m.clusterList.table, cmd = m.clusterList.table.Update(msg)
				m.clusterList.list, _ = m.clusterList.list.Update(msg) // Keep list for search functionality

				// Check if selection changed and auto-load details for right pane
				if m.clusterList.table.Cursor() != oldCursor {
					newCmd := m.updateSelectedClusterDetails()
					if newCmd != nil {
						cmd = tea.Batch(cmd, newCmd)
					}
				}
			} else {
				// Right pane: Update detail table
				// Always pass key messages to the table, it will handle empty state gracefully
				if len(m.clusterList.detailView.table.Rows()) > 0 {
					m.clusterList.detailView.table, cmd = m.clusterList.detailView.table.Update(msg)
				}
			}
		}

	}

	return m, cmd
}

// Handle main menu selection
func (m AppModel) handleMainMenuSelection() (AppModel, tea.Cmd) {
	item := m.mainMenu.list.SelectedItem().(MainMenuItem)

	switch item.action {
	case "list-clusters":
		m.previousScreen = MainMenuScreen
		m.screen = ClusterListScreen
		// Don't restart loading if already in progress or completed
		if !m.clusterList.loading && len(m.clusterList.list.Items()) == 0 {
			// Only start loading if not already loading and no clusters loaded
			m.clusterList.loading = true
			return m, tea.Batch(m.clusterList.spinner.Tick, m.loadClustersCmd())
		}
		// If loading is in progress, continue the spinner tick
		if m.clusterList.loading {
			return m, m.clusterList.spinner.Tick
		}
		return m, nil
	default:
		// For now, just show a placeholder
		m.error = fmt.Sprintf("Feature '%s' coming soon!", item.title)
		return m, nil
	}
}

// Message for updating selected cluster details
type ClusterSelectionChangedMsg struct {
	clusterName string
	cluster     ClusterItem
}

// Update cluster details when selection changes in multi-pane mode
func (m AppModel) updateSelectedClusterDetails() tea.Cmd {
	selectedIndex := m.clusterList.table.Cursor()

	var cluster ClusterItem

	if m.clusterList.searching {
		// When searching, we need to map from filtered results back to original clusters
		filteredRows := m.clusterList.table.Rows()
		if selectedIndex >= len(filteredRows) {
			return nil
		}

		// Get the cluster name from the filtered row
		selectedRow := filteredRows[selectedIndex]
		if len(selectedRow) == 0 {
			return nil
		}
		clusterName := selectedRow[0] // First column is cluster name

		// Find the matching cluster in the original list
		found := false
		for _, c := range m.clusterList.clusters {
			if c.name == clusterName {
				cluster = c
				found = true
				break
			}
		}

		if !found {
			return nil
		}
	} else {
		// Normal mode - direct index mapping
		if selectedIndex >= len(m.clusterList.clusters) {
			return nil
		}
		cluster = m.clusterList.clusters[selectedIndex]
	}

	// Return a message to update the selection
	return func() tea.Msg {
		return ClusterSelectionChangedMsg{
			clusterName: cluster.name,
			cluster:     cluster,
		}
	}
}

// Refresh cluster list - clears cache and reloads everything
func (m AppModel) refreshClusterList() (AppModel, tea.Cmd) {
	// Clear cache and reset state
	m.clusterList.clusterInfo = make(map[string]*ClusterInfo)
	m.clusterList.clusterPasswords = make(map[string]string) // Clear password cache too
	m.clusterList.clusters = []ClusterItem{}
	m.clusterList.list.SetItems([]list.Item{})
	m.clusterList.table.SetRows([]table.Row{})
	m.clusterList.filteredRows = []table.Row{}
	m.clusterList.loading = true
	m.clusterList.searching = false
	m.clusterList.searchInput.SetValue("")
	m.clusterList.searchInput.Blur()
	m.error = "" // Clear any previous errors

	// Start fresh loading
	return m, tea.Batch(m.clusterList.spinner.Tick, m.loadClustersCmd())
}

// Refresh single cluster - reloads only the currently selected cluster
func (m AppModel) refreshSingleCluster() (AppModel, tea.Cmd) {
	// Get currently selected cluster
	selectedIndex := m.clusterList.table.Cursor()
	if selectedIndex >= len(m.clusterList.clusters) {
		return m, showNotification("No cluster selected", true)
	}

	selectedCluster := m.clusterList.clusters[selectedIndex]
	if !selectedCluster.accessible {
		return m, showNotification("Cannot refresh inaccessible cluster", true)
	}

	// Clear cache for this specific cluster
	delete(m.clusterList.clusterInfo, selectedCluster.name)
	delete(m.clusterList.clusterPasswords, selectedCluster.name)

	// Update the cluster item to show loading state in the left table
	m.clusterList.clusters[selectedIndex] = ClusterItem{
		name:       selectedCluster.name,
		status:     "Loading",
		ocpVersion: "", // Blank during loading
		mtvVersion: "", // Blank during loading
		cnvVersion: "", // Blank during loading
		accessible: true,
	}

	// Update the table rows to reflect the loading state
	m.updateClusterTableRows()

	// Instead of clearing the detail view, mark it as updating and recreate table with "Updating..." values
	if m.clusterList.detailView.info != nil {
		m.clusterList.detailView.updating = true
		// Recreate the table with "Updating..." values
		rightWidth := (m.width - 4) * 7 / 10 // Calculate 70% of available width
		if rightWidth < 40 {
			rightWidth = 40 // Minimum width for readability
		}
		m.setupRightPaneTable(rightWidth - 6)
	}

	return m, tea.Batch(
		m.loadSingleClusterCmd(selectedCluster.name),
		showNotification(fmt.Sprintf("Refreshing %s...", selectedCluster.name), false),
	)
}

// Helper function to update cluster table rows from clusters slice
func (m *AppModel) updateClusterTableRows() {
	var rows []table.Row
	for _, cluster := range m.clusterList.clusters {
		var status string
		if cluster.accessible && cluster.status == "Loading" {
			status = "🔄 Loading"
		} else if cluster.accessible {
			// All accessible clusters should show as Online, regardless of MTV status
			status = "✅ Online"
		} else {
			if cluster.status == "Timeout" {
				status = "⏰ Timeout"
			} else {
				status = "❌ Offline"
			}
		}

		// Only include cluster name and status in the left pane table
		row := table.Row{
			cluster.name,
			status,
		}
		rows = append(rows, row)
	}

	// Store filtered rows for search functionality
	m.clusterList.filteredRows = rows
	m.clusterList.table.SetRows(rows)
}

// Command to load clusters asynchronously - now with real data
func (m AppModel) loadClustersCmd() tea.Cmd {
	return func() tea.Msg {
		// Read cluster directories
		clusterDirs, err := clusterLoaderDeps.ReadDir(CLUSTERS_PATH)
		if err != nil {
			// Return empty list on error - this will show "No clusters found"
			return ClustersLoadedMsg{
				clusters:    []ClusterItem{},
				clusterInfo: make(map[string]*ClusterInfo),
			}
		}

		// Filter cluster names
		var clusterNames []string
		for _, entry := range clusterDirs {
			if !entry.IsDir() {
				continue
			}
			name := entry.Name()
			if strings.HasPrefix(name, "qemtv-") || strings.HasPrefix(name, "qemtvd-") {
				clusterNames = append(clusterNames, name)
			}
		}

		if len(clusterNames) == 0 {
			return ClustersLoadedMsg{
				clusters:    []ClusterItem{},
				clusterInfo: make(map[string]*ClusterInfo),
			}
		}

		// Concurrent cluster loading (similar to CLI implementation)
		type clusterResult struct {
			info ClusterInfo
			err  error
		}

		resultChan := make(chan clusterResult, len(clusterNames))
		var mu sync.Mutex
		var clusters []ClusterItem
		clusterInfoMap := make(map[string]*ClusterInfo)

		// Launch goroutine for each cluster
		for _, clusterName := range clusterNames {
			go func(name string) {
				defer func() {
					if r := recover(); r != nil {
						resultChan <- clusterResult{err: fmt.Errorf("panic in %s: %v", name, r)}
					}
				}()

				// Try to ensure logged in and get cluster info
				if err := clusterLoaderDeps.EnsureLoggedInSilent(name); err != nil {
					resultChan <- clusterResult{err: fmt.Errorf("login failed for %s: %w", name, err)}
					return
				}

				info, err := clusterLoaderDeps.GetClusterInfoSilent(name)
				if err != nil {
					resultChan <- clusterResult{err: fmt.Errorf("cluster info failed for %s: %w", name, err)}
					return
				}

				resultChan <- clusterResult{info: *info}
			}(clusterName)
		}

		// Collect results with timeout
		collected := 0
		timeout := time.After(60 * time.Second) // Shorter timeout for TUI
		for collected < len(clusterNames) {
			select {
			case result := <-resultChan:
				if result.err == nil {
					// Convert ClusterInfo to ClusterItem
					item := ClusterItem{
						name:       result.info.Name,
						accessible: true,
						ocpVersion: result.info.OCPVersion,
						mtvVersion: result.info.MTVVersion,
						cnvVersion: result.info.CNVVersion,
					}
					// Set status as Online for all accessible clusters
					item.status = "Online"

					mu.Lock()
					clusters = append(clusters, item)
					clusterInfoMap[result.info.Name] = &result.info // Cache full cluster info
					mu.Unlock()
				} else {
					// Add inaccessible cluster
					clusterName := extractClusterNameFromError(result.err.Error())
					if clusterName == "" {
						// Try to extract from error, or skip
						continue
					}
					item := ClusterItem{
						name:       clusterName,
						accessible: false,
						status:     "Offline",
						ocpVersion: "",
						mtvVersion: "",
						cnvVersion: "",
					}

					mu.Lock()
					clusters = append(clusters, item)
					mu.Unlock()
				}
				collected++

			case <-timeout:
				// Add remaining clusters as offline
				mu.Lock()
				addedNames := make(map[string]bool)
				for _, cluster := range clusters {
					addedNames[cluster.name] = true
				}
				for _, name := range clusterNames {
					if !addedNames[name] {
						clusters = append(clusters, ClusterItem{
							name:       name,
							accessible: false,
							status:     "Timeout",
							ocpVersion: "",
							mtvVersion: "",
							cnvVersion: "",
						})
					}
				}
				mu.Unlock()
				goto done
			}
		}

	done:
		// Sort clusters by name for consistent display
		sort.Slice(clusters, func(i, j int) bool {
			return clusters[i].name < clusters[j].name
		})

		return ClustersLoadedMsg{
			clusters:    clusters,
			clusterInfo: clusterInfoMap,
		}
	}
}

// Helper function to extract cluster name from error messages
func extractClusterNameFromError(errorMsg string) string {
	// Try to extract cluster name from error messages like "login failed for qemtv-01: ..."
	if strings.Contains(errorMsg, "login failed for ") {
		parts := strings.Split(errorMsg, "login failed for ")
		if len(parts) > 1 {
			namePart := strings.Split(parts[1], ":")[0]
			return strings.TrimSpace(namePart)
		}
	}
	if strings.Contains(errorMsg, "cluster info failed for ") {
		parts := strings.Split(errorMsg, "cluster info failed for ")
		if len(parts) > 1 {
			namePart := strings.Split(parts[1], ":")[0]
			return strings.TrimSpace(namePart)
		}
	}
	return ""
}

// View renders the current screen using full terminal size
func (m AppModel) View() string {
	if m.width == 0 || m.height == 0 {
		return "Loading..."
	}

	var content strings.Builder

	// Header - full width
	header := HeaderContainerFull(Title("🚀 MTV API Test Developer Tool"), m.width)
	content.WriteString(header)

	// Main content area
	var mainContent string
	switch m.screen {
	case MainMenuScreen:
		mainContent = m.renderMainMenu()
	case ClusterListScreen:
		mainContent = m.renderClusterList()
	case ClusterDetailScreen:
		mainContent = m.renderClusterDetail()
	}

	// Add main content with proper centering
	if m.screen == ClusterListScreen {
		// For cluster list screen, use full width - no containers
		content.WriteString(mainContent)
	} else {
		// For other screens (main menu, cluster detail), center the content manually
		// Calculate padding for horizontal centering
		lines := strings.Split(mainContent, "\n")
		var centeredLines []string

		for _, line := range lines {
			if strings.TrimSpace(line) == "" {
				centeredLines = append(centeredLines, line) // Keep empty lines as-is
			} else {
				// Center each non-empty line
				lineWidth := len(strings.ReplaceAll(line, "\t", "    ")) // Convert tabs to spaces for width calc
				if lineWidth < m.width {
					leftPadding := (m.width - lineWidth) / 2
					centeredLine := strings.Repeat(" ", leftPadding) + line
					centeredLines = append(centeredLines, centeredLine)
				} else {
					centeredLines = append(centeredLines, line) // Line too long, don't pad
				}
			}
		}

		centeredContent := strings.Join(centeredLines, "\n")
		content.WriteString(centeredContent)
	}

	// Status bar overlay (always visible at bottom before footer)
	var statusBar string
	if m.notification != "" {
		statusBar = lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Foreground(lipgloss.Color("32")).
			Background(lipgloss.Color("240")).
			Render("📋 " + m.notification)
	} else if m.error != "" {
		statusBar = lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Foreground(lipgloss.Color("196")).
			Background(lipgloss.Color("240")).
			Render("❌ " + m.error)
	} else {
		// Empty status bar to maintain consistent spacing
		statusBar = lipgloss.NewStyle().
			Width(m.width).
			Height(1).
			Background(lipgloss.Color("240")).
			Render(" ")
	}

	// Footer - full width
	footer := FooterContainerFull(m.help.View(m.keys), m.width)

	// Assemble final layout with status bar at bottom
	finalContent := content.String() + "\n" + statusBar + "\n" + footer

	// Apply vertical centering for non-cluster-list screens
	if m.screen != ClusterListScreen && m.screen != MainMenuScreen {
		lines := strings.Count(finalContent, "\n") + 1
		if lines < m.height {
			topPadding := (m.height - lines) / 3 // Position in upper third
			finalContent = strings.Repeat("\n", topPadding) + finalContent
		}
	}

	return finalContent
}

// Render main menu
func (m AppModel) renderMainMenu() string {
	// Calculate available content height (subtract header, status bar, footer)
	contentHeight := m.height - 6 // Reserve space for header, status, footer
	if contentHeight < 10 {
		contentHeight = 10 // Minimum usable height
	}

	var content strings.Builder

	// Add some top spacing to center content vertically
	topSpacing := contentHeight / 4
	if topSpacing > 0 {
		content.WriteString(strings.Repeat("\n", topSpacing))
	}

	// Main menu title - centered
	title := "MTV Dev Tool"
	centeredTitle := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render(title)
	content.WriteString(centeredTitle + "\n\n\n") // Extra spacing after title

	// Menu items - manually centered
	items := []string{"📋 Clusters"}
	selectedIndex := m.mainMenu.list.Index()

	for i, item := range items {
		var styledItem string
		if i == selectedIndex {
			styledItem = selectedItemStyle.Render(item)
		} else {
			styledItem = menuItemStyle.Render(item)
		}

		// Center each menu item
		centeredItem := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(styledItem)
		content.WriteString(centeredItem + "\n\n") // Extra spacing between items
	}

	// Add vertical spacing before status indicators
	content.WriteString("\n\n")

	// Add background loading indicator with spinner
	if m.clusterList.loading {
		// Check if this is initial load or refresh
		if len(m.clusterList.list.Items()) == 0 {
			loadingIndicator := StatusLoading("Loading clusters in background...")
			centeredLoading := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(loadingIndicator)
			content.WriteString(centeredLoading + "\n\n")

			spinnerView := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(m.clusterList.spinner.View())
			content.WriteString(spinnerView)
		} else {
			loadingIndicator := StatusLoading("Refreshing clusters...")
			centeredLoading := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(loadingIndicator)
			content.WriteString(centeredLoading + "\n\n")

			spinnerView := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(m.clusterList.spinner.View())
			content.WriteString(spinnerView)
		}
	}

	// Show cluster count if loaded
	if len(m.clusterList.list.Items()) > 0 {
		clusterCount := len(m.clusterList.list.Items())
		readyIndicator := fmt.Sprintf("✅ %d clusters ready", clusterCount)
		centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(readyIndicator)
		content.WriteString(centeredIndicator + "\n\n")

		// Add instructions for main menu
		instructions := "💡 Press Enter to view clusters • Ctrl+R to refresh"
		centeredInstructions := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Foreground(lipgloss.Color("240")).
			Render(instructions)
		content.WriteString(centeredInstructions)
	}

	// Add bottom spacing to fill the screen
	currentLines := strings.Count(content.String(), "\n") + 1
	remainingLines := contentHeight - currentLines
	if remainingLines > 0 {
		content.WriteString(strings.Repeat("\n", remainingLines))
	}

	return content.String()
}

// Render cluster list with working multi-pane layout
func (m AppModel) renderClusterList() string {
	if m.clusterList.loading {
		var content strings.Builder

		// Check if this is initial load or refresh
		var loadingText string
		if len(m.clusterList.list.Items()) == 0 {
			loadingText = "🔍 Scanning OpenShift Clusters..."
		} else {
			loadingText = "🔄 Refreshing Cluster Information..."
		}

		// Build the loading content
		loadingContent := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(Header(loadingText))

		content.WriteString(loadingContent + "\n\n")

		// Center the discovery text
		discoveryText := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render("🔎 Discovering and connecting to clusters...")
		content.WriteString(discoveryText + "\n\n")

		// Center the spinner
		spinnerText := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(m.clusterList.spinner.View())
		content.WriteString(spinnerText)

		return content.String()
	}

	// Multi-pane layout: Left = Cluster Table, Right = Cluster Details
	// Use FULL terminal width - no artificial constraints
	totalWidth := m.width - 4            // Account for borders and spacing
	leftWidth := totalWidth * 3 / 10     // ~30% for cluster table (smaller since only name + status)
	rightWidth := totalWidth - leftWidth // ~70% for details (more space for detailed info)

	// Only fallback if terminal is genuinely too small
	if totalWidth < 80 {
		return m.renderSinglePaneClusterList()
	}

	// LEFT PANE: Cluster Table (not too compact)
	var leftContent strings.Builder

	// Title with focus indicator
	title := "Clusters"
	if m.clusterList.focusedPane == 0 {
		title = "🎯 " + title + " (Navigate clusters)"
	}
	leftContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(title) + "\n\n")

	// Search input
	if m.clusterList.searching {
		leftContent.WriteString("Search: " + m.clusterList.searchInput.View() + "\n")
	}

	// Use proportional column widths based on left pane width
	availableTableWidth := leftWidth - 6 // Account for padding and borders
	tableColumns := []table.Column{
		{Title: "Cluster", Width: availableTableWidth * 6 / 10}, // 60% - more space for cluster names
		{Title: "Status", Width: availableTableWidth * 4 / 10},  // 40% - adequate space for status
	}

	// Use original table rows - let the table handle layout
	leftTable := table.New(
		table.WithColumns(tableColumns),
		table.WithRows(m.clusterList.table.Rows()),
		table.WithFocused(m.clusterList.focusedPane == 0), // Only focused if left pane is active
		// NO table.WithHeight() - let it size naturally to show all clusters
	)
	leftTable.SetCursor(m.clusterList.table.Cursor())

	// Clean table styling
	tableStyles := table.DefaultStyles()
	tableStyles.Header = tableStyles.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		BorderBottom(true).
		Bold(true)
	tableStyles.Selected = tableStyles.Selected.
		Foreground(lipgloss.Color("229")).
		Background(lipgloss.Color("57")).
		Bold(false)
	leftTable.SetStyles(tableStyles)

	leftContent.WriteString(leftTable.View())

	// RIGHT PANE: Simple cluster details
	rightContent := m.renderSimpleClusterDetails(rightWidth - 6) // Account for border and padding

	// Create bordered panes
	leftPane := lipgloss.NewStyle().
		Width(leftWidth).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(0, 1).
		Render(leftContent.String())

	rightPane := lipgloss.NewStyle().
		Width(rightWidth).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(0, 1).
		Render(rightContent)

	// Join panes side by side
	layout := lipgloss.JoinHorizontal(lipgloss.Top, leftPane, "  ", rightPane)

	// Add instructions based on focused pane
	var instructions string
	if m.clusterList.focusedPane == 0 {
		instructions = "\n\n💡 Press / to search • ↑↓ navigate clusters • Tab to switch to details pane • Ctrl+U refresh single cluster"
	} else {
		instructions = "\n\n💡 ↑↓ navigate fields • Enter to copy to clipboard • Tab to switch to clusters pane"
	}
	return layout + instructions
}

// Fallback single pane layout for narrow terminals
func (m AppModel) renderSinglePaneClusterList() string {
	var content strings.Builder
	content.WriteString(Header("Available Clusters") + "\n\n")

	// Show search input if in search mode
	if m.clusterList.searching {
		content.WriteString("Search: " + m.clusterList.searchInput.View() + "\n\n")
	}

	content.WriteString(m.clusterList.table.View())

	// Add instructions
	var instruction string
	if m.clusterList.searching {
		instruction = "\n\n💡 Type to search • Esc to exit search • Enter to select"
	} else {
		instruction = "\n\n💡 Press / to search • ↑↓ to navigate • Enter to select"
	}
	content.WriteString(instruction)

	return content.String()
}

// Navigable table for right pane cluster details
func (m AppModel) renderSimpleClusterDetails(maxWidth int) string {
	if m.clusterList.detailView.loading {
		return "Loading cluster details...\n\n⏳"
	}

	if m.clusterList.detailView.info == nil {
		return "Select a cluster to view details"
	}

	// Setup table for right pane if not already done or if table is empty
	if len(m.clusterList.detailView.table.Rows()) == 0 {
		m.setupRightPaneTable(maxWidth)
	}

	var content strings.Builder

	// Title with focus indicator
	title := "Cluster Details"
	if m.clusterList.focusedPane == 1 {
		title = "🎯 " + title + " (Press Enter to copy)"
	}

	content.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("33")).
		Render(title) + "\n\n")

	// Cluster name
	content.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render("🖥️  "+m.clusterList.detailView.info.Name) + "\n\n")

	// Show the navigable table
	content.WriteString(m.clusterList.detailView.table.View())

	return content.String()
}

// renderClusterOperations function removed - we go directly to cluster details now

// Render cluster detail (legacy - kept for compatibility)
func (m AppModel) renderClusterDetail() string {
	if m.clusterDetail.loading {
		loadingContent := StatusLoading("Loading cluster details...") + "\n\n" + m.clusterDetail.spinner.View()
		return CenteredContainer(loadingContent, 40)
	}

	if m.clusterDetail.info == nil {
		return "No cluster information available."
	}

	var content strings.Builder

	// Title matching CLI format
	content.WriteString(Header(fmt.Sprintf("OpenShift Cluster Info -- [%s]", m.clusterDetail.info.Name)) + "\n\n")

	// Use the initialized table from the model
	content.WriteString(m.clusterDetail.table.View())

	// Add copy instruction
	content.WriteString("\n\n💡 Use ↑↓ to navigate • Enter to copy value to clipboard")

	return content.String()
}

// Custom delegates for list rendering
type MainMenuDelegate struct{}

func (d MainMenuDelegate) Height() int                             { return 1 }
func (d MainMenuDelegate) Spacing() int                            { return 0 }
func (d MainMenuDelegate) Update(_ tea.Msg, _ *list.Model) tea.Cmd { return nil }
func (d MainMenuDelegate) Render(w io.Writer, m list.Model, index int, item list.Item) {
	i, ok := item.(MainMenuItem)
	if !ok {
		return
	}

	str := fmt.Sprintf("  %s", i.title)
	if index == m.Index() {
		_, _ = fmt.Fprint(w, selectedItemStyle.Render(str))
	} else {
		_, _ = fmt.Fprint(w, menuItemStyle.Render(str))
	}
}

type ClusterDelegate struct{}

func (d ClusterDelegate) Height() int                             { return 1 }
func (d ClusterDelegate) Spacing() int                            { return 0 }
func (d ClusterDelegate) Update(_ tea.Msg, _ *list.Model) tea.Cmd { return nil }
func (d ClusterDelegate) Render(w io.Writer, m list.Model, index int, item list.Item) {
	i, ok := item.(ClusterItem)
	if !ok {
		return
	}

	// Format cluster information in a table-like structure
	statusIcon := "❌"
	statusText := "Offline"

	if i.accessible {
		if i.mtvVersion == "Not installed" || i.mtvVersion == "" {
			statusIcon = "⚠️"
			statusText = "No MTV"
		} else {
			statusIcon = "✅"
			statusText = "Online"
		}
	} else {
		if i.status == "Timeout" {
			statusIcon = "⏰"
			statusText = "Timeout"
		}
	}

	// Create consistent column widths for table-like appearance
	nameCol := fmt.Sprintf("%-12s", i.name)
	statusCol := fmt.Sprintf("%s %-8s", statusIcon, statusText)

	var ocpCol, mtvCol string
	if i.accessible {
		ocpCol = fmt.Sprintf("OCP: %-6s", i.ocpVersion)
		if i.mtvVersion == "Not installed" || i.mtvVersion == "" {
			mtvCol = "MTV: N/A"
		} else {
			mtvCol = fmt.Sprintf("MTV: %-6s", i.mtvVersion)
		}
	} else {
		ocpCol = "OCP: N/A    "
		mtvCol = "MTV: N/A"
	}

	// Left-aligned table format
	tableRow := fmt.Sprintf("%s │ %s │ %s │ %s", nameCol, statusCol, ocpCol, mtvCol)

	if index == m.Index() {
		_, _ = fmt.Fprint(w, selectedItemStyle.Render(tableRow))
	} else {
		_, _ = fmt.Fprint(w, tableRow)
	}
}

// Command to load cluster password
func (m AppModel) loadClusterPasswordCmd(clusterName string) tea.Cmd {
	return func() tea.Msg {
		password, err := clusterLoaderDeps.GetClusterPassword(clusterName)
		return ClusterPasswordLoadedMsg{
			clusterName: clusterName,
			password:    password,
			err:         err,
		}
	}
}

// Command to load cluster details for various operations
func (m AppModel) loadClusterDetailCmd(clusterName, operation string) tea.Cmd {
	return func() tea.Msg {
		// Get cluster info
		info, err := clusterLoaderDeps.GetClusterInfoSilent(clusterName)
		if err != nil {
			return ClusterDetailLoadedMsg{err: err}
		}

		// Get password for login command
		password, err := clusterLoaderDeps.GetClusterPassword(clusterName)
		if err != nil {
			return ClusterDetailLoadedMsg{err: err}
		}

		// Generate login command
		apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", clusterName)
		loginCmd := fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, password)

		return ClusterDetailLoadedMsg{
			info:     info,
			password: password,
			loginCmd: loginCmd,
			err:      nil,
		}
	}
}

// Handle cluster detail table copy for cluster detail screen
func (m AppModel) handleClusterDetailTableCopy() (AppModel, tea.Cmd) {
	selectedIndex := m.clusterDetail.table.Cursor()
	rows := m.clusterDetail.table.Rows()

	if selectedIndex >= len(rows) {
		m.error = "No row selected"
		return m, nil
	}

	selectedRow := rows[selectedIndex]
	if len(selectedRow) < 2 {
		m.error = "Invalid row data"
		return m, nil
	}

	fieldName := selectedRow[0]
	valueToCopy := selectedRow[1]

	// Copy to clipboard
	if err := clipboardWriteAll(valueToCopy); err != nil {
		return m, showNotification(fmt.Sprintf("Failed to copy: %v", err), true)
	} else {
		return m, showNotification(fmt.Sprintf("Copied %s", fieldName), false)
	}
}

// Handle copy from right pane in multi-pane cluster list
func (m AppModel) handleRightPaneCopy() (AppModel, tea.Cmd) {
	if m.clusterList.detailView.info == nil {
		m.error = "No cluster information available"
		return m, nil
	}

	// Get the selected row from the detail table
	selectedIndex := m.clusterList.detailView.table.Cursor()
	rows := m.clusterList.detailView.table.Rows()

	if selectedIndex >= len(rows) {
		m.error = "No field selected"
		return m, nil
	}

	selectedRow := rows[selectedIndex]
	if len(selectedRow) < 2 {
		m.error = "Invalid field data"
		return m, nil
	}

	fieldName := selectedRow[0]
	valueToCopy := selectedRow[1]

	// Copy to clipboard
	if err := clipboardWriteAll(valueToCopy); err != nil {
		return m, showNotification(fmt.Sprintf("Failed to copy: %v", err), true)
	} else {
		return m, showNotification(fmt.Sprintf("Copied %s", fieldName), false)
	}
}

// Filter clusters based on search input
func (m AppModel) filterClusters(query string) []table.Row {
	if query == "" {
		return m.clusterList.filteredRows
	}

	query = strings.ToLower(query)
	var filteredRows []table.Row

	for _, row := range m.clusterList.filteredRows {
		// Search in all columns
		found := false
		for _, cell := range row {
			if strings.Contains(strings.ToLower(cell), query) {
				found = true
				break
			}
		}
		if found {
			filteredRows = append(filteredRows, row)
		}
	}

	return filteredRows
}

// Setup cluster detail table with all the cluster information
func (m *AppModel) setupClusterDetailTable() {
	if m.clusterDetail.info == nil {
		return
	}

	info := m.clusterDetail.info

	// Create table columns
	columns := []table.Column{
		{Title: "Field", Width: 12},
		{Title: "Value", Width: 60},
	}

	// Create table rows
	rows := []table.Row{
		{"Username", "kubeadmin"},
	}

	if m.clusterDetail.password != "" {
		rows = append(rows, table.Row{"Password", m.clusterDetail.password})
	}

	rows = append(rows, table.Row{"Console", info.ConsoleURL})
	rows = append(rows, table.Row{"OCP version", info.OCPVersion})

	// MTV version with IIB if available (matching CLI exactly)
	mtvDisplay := info.MTVVersion
	if info.IIB != "N/A" && info.IIB != "" && info.MTVVersion != "Not installed" {
		mtvDisplay = fmt.Sprintf("%s (%s)", info.MTVVersion, info.IIB)
	}
	rows = append(rows, table.Row{"MTV version", mtvDisplay})
	rows = append(rows, table.Row{"CNV version", info.CNVVersion})

	if m.clusterDetail.loginCmd != "" {
		rows = append(rows, table.Row{"Login", m.clusterDetail.loginCmd})
	}

	// Create table with proper styling
	t := table.New(
		table.WithColumns(columns),
		table.WithRows(rows),
		table.WithFocused(true), // Enable focus for navigation
		table.WithHeight(len(rows)),
	)

	// Style the table to look clean
	s := table.DefaultStyles()
	s.Header = s.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		BorderBottom(true).
		Bold(false)
	s.Selected = s.Selected.
		Foreground(lipgloss.Color("229")).
		Background(lipgloss.Color("57")).
		Bold(false)
	t.SetStyles(s)

	// Set the table in the model
	m.clusterDetail.table = t
}

// Setup navigable table for right pane cluster details
func (m *AppModel) setupRightPaneTable(maxWidth int) {
	if m.clusterList.detailView.info == nil {
		return
	}

	info := m.clusterList.detailView.info

	// Create table columns for right pane - give more space to values
	fieldWidth := 12
	valueWidth := maxWidth - fieldWidth - 6 // Account for borders and spacing
	if valueWidth < 30 {
		valueWidth = 30
	}

	columns := []table.Column{
		{Title: "Field", Width: fieldWidth},
		{Title: "Value", Width: valueWidth},
	}

	// Create table rows with cluster information - show "Updating..." if updating
	var rows []table.Row

	if m.clusterList.detailView.updating {
		// Show "Updating..." for all values during refresh
		rows = append(rows, table.Row{"OCP Version", "Updating..."})
		rows = append(rows, table.Row{"MTV Version", "Updating..."})
		rows = append(rows, table.Row{"CNV Version", "Updating..."})
		rows = append(rows, table.Row{"Console URL", "Updating..."})
		rows = append(rows, table.Row{"Username", "Updating..."})
		rows = append(rows, table.Row{"Password", "Updating..."})
		rows = append(rows, table.Row{"Login Cmd", "Updating..."})
	} else {
		// Show actual values when not updating
		rows = append(rows, table.Row{"OCP Version", info.OCPVersion})
		rows = append(rows, table.Row{"MTV Version", info.MTVVersion})
		rows = append(rows, table.Row{"CNV Version", info.CNVVersion})

		// Store FULL console URL for copying (table will handle display truncation)
		rows = append(rows, table.Row{"Console URL", info.ConsoleURL})
		rows = append(rows, table.Row{"Username", "kubeadmin"})

		// Add password if available
		if m.clusterList.detailView.password != "" {
			rows = append(rows, table.Row{"Password", m.clusterList.detailView.password})
		}

		// Store FULL login command for copying (table will handle display truncation)
		if m.clusterList.detailView.loginCmd != "" {
			rows = append(rows, table.Row{"Login Cmd", m.clusterList.detailView.loginCmd})
		}
	}

	// Create table WITHOUT height constraint to prevent scroll bars
	t := table.New(
		table.WithColumns(columns),
		table.WithRows(rows),
		table.WithFocused(true), // Always enable focus for navigation
		// NO table.WithHeight() - let it size naturally
	)

	// Style the table
	s := table.DefaultStyles()
	s.Header = s.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		BorderBottom(true).
		Bold(false)
	s.Selected = s.Selected.
		Foreground(lipgloss.Color("229")).
		Background(lipgloss.Color("57")).
		Bold(false)
	t.SetStyles(s)

	// Set the table in the model
	m.clusterList.detailView.table = t
}

// Command to load a single cluster asynchronously
func (m AppModel) loadSingleClusterCmd(clusterName string) tea.Cmd {
	return func() tea.Msg {
		// Try to ensure logged in and get cluster info
		if err := clusterLoaderDeps.EnsureLoggedInSilent(clusterName); err != nil {
			return ClusterDetailLoadedMsg{
				err: fmt.Errorf("login failed for %s: %w", clusterName, err),
			}
		}

		info, err := clusterLoaderDeps.GetClusterInfoSilent(clusterName)
		if err != nil {
			return ClusterDetailLoadedMsg{
				err: fmt.Errorf("cluster info failed for %s: %w", clusterName, err),
			}
		}

		// Also get password
		password, err := clusterLoaderDeps.GetClusterPassword(clusterName)
		if err != nil {
			return ClusterDetailLoadedMsg{
				err: fmt.Errorf("password failed for %s: %w", clusterName, err),
			}
		}

		// Generate login command
		apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", info.Name)
		loginCmd := fmt.Sprintf("oc login --insecure-skip-tls-verify=true %s -u kubeadmin -p %s", apiURL, password)

		return ClusterDetailLoadedMsg{
			info:     info,
			password: password,
			loginCmd: loginCmd,
		}
	}
}
