package tui

import (
	"context"
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

// IIB build information
type IIBInfo struct {
	OCPVersion  string
	MTVVersion  string
	IIB         string
	Snapshot    string
	Created     string
	Image       string
	Environment string
}

// VMInfo represents basic information about a virtual machine (local TUI copy)
type VMInfo struct {
	Name         string
	Provider     string
	PowerState   string
	UUID         string
	CPU          int32
	MemoryMB     int64
	GuestOS      string
	IPAddresses  []string
	StorageGB    float64
	CreationTime *time.Time
	LastModified *time.Time
	Tags         []string
	Cluster      string
	Host         string
	ResourcePool string
	Networks     []string
}

// Helper function interfaces to access main package functionality
type ClusterLoaderDeps interface {
	ReadDir(path string) ([]fs.DirEntry, error)
	EnsureLoggedInSilent(clusterName string) error
	GetClusterInfoSilent(clusterName string) (*ClusterInfo, error)
	GetClusterPassword(clusterName string) (string, error)
}

// IIB data loader interface to access main package IIB functions
type IIBLoaderDeps interface {
	GetForkliftBuilds(environment string) ([]IIBInfo, error)
	CheckKufloxLogin() bool
	LoginToKuflox() error
}

type ProviderConfig struct {
	Type        string
	URL         string
	Username    string
	Password    string
	Insecure    bool
	ExtraParams map[string]string
}

type VMProvider interface {
	Connect() error
	ListVMs(ctx context.Context) ([]VMInfo, error)
	GetVM(ctx context.Context, name string) (*VMInfo, error)
	Close() error
	GetProviderType() string
}

type ProviderLoaderDeps interface {
	LoadProviderConfigs() (map[string]ProviderConfig, error)
	CreateProvider(providerType, url, username, password string, insecure bool, extraParams map[string]string) (VMProvider, error)
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

// Default IIB implementation (mock for testing)
type defaultIIBLoaderDeps struct{}

func (d *defaultIIBLoaderDeps) GetForkliftBuilds(environment string) ([]IIBInfo, error) {
	// This will be injected from main package
	return nil, fmt.Errorf("getForkliftBuilds not available in TUI context")
}

func (d *defaultIIBLoaderDeps) CheckKufloxLogin() bool {
	// This will be injected from main package
	return false
}

func (d *defaultIIBLoaderDeps) LoginToKuflox() error {
	// This will be injected from main package
	return fmt.Errorf("loginToKuflox not available in TUI context")
}

// Default provider loader implementation
type defaultProviderLoaderDeps struct{}

func (d *defaultProviderLoaderDeps) LoadProviderConfigs() (map[string]ProviderConfig, error) {
	// This will be injected from main package
	return nil, fmt.Errorf("loadProviderConfigs not available in TUI context")
}

func (d *defaultProviderLoaderDeps) CreateProvider(providerType, url, username, password string, insecure bool, extraParams map[string]string) (VMProvider, error) {
	// This will be injected from main package
	return nil, fmt.Errorf("createProvider not available in TUI context")
}

// Global dependency injection
var clusterLoaderDeps ClusterLoaderDeps = &defaultClusterLoaderDeps{}
var iibLoaderDeps IIBLoaderDeps = &defaultIIBLoaderDeps{}
var providerLoaderDeps ProviderLoaderDeps = &defaultProviderLoaderDeps{}

// SetClusterLoaderDeps allows injecting dependencies from main package
func SetClusterLoaderDeps(deps ClusterLoaderDeps) {
	clusterLoaderDeps = deps
}

// SetIIBLoaderDeps allows injecting IIB dependencies from main package
func SetIIBLoaderDeps(deps IIBLoaderDeps) {
	iibLoaderDeps = deps
}

// SetProviderLoaderDeps allows injecting provider dependencies from main package
func SetProviderLoaderDeps(deps ProviderLoaderDeps) {
	providerLoaderDeps = deps
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
	IIBInputScreen
	IIBDisplayScreen
	ThemeSelectionScreen
	ProvidersScreen
	ProviderDetailScreen
)

// Application state
type AppModel struct {
	screen          ScreenType
	previousScreen  ScreenType // Track navigation history
	selectedCluster string

	mainMenu          MainMenuModel
	clusterList       ClusterListModel
	clusterDetail     ClusterDetailModel
	iibInput          IIBInputModel
	iibDisplay        IIBDisplayModel
	themeSelection    ThemeSelectionModel
	providers         ProvidersModel
	providerDetail    ProviderDetailModel
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

// Provider detail model
type ProviderDetailModel struct {
	providerName string
	config       *ProviderConfig
	loading      bool
	spinner      spinner.Model
	table        table.Model
}

// IIB Input model for entering MTV version
type IIBInputModel struct {
	textInput  textinput.Model
	mtvVersion string
	loading    bool
	spinner    spinner.Model
}

// IIB Item for the build type and OCP version lists
type IIBItem struct {
	name        string
	displayName string
}

func (i IIBItem) FilterValue() string { return i.name }
func (i IIBItem) Title() string       { return i.displayName }
func (i IIBItem) Description() string { return "" }

// IIB Display model for three-panel layout
type IIBDisplayModel struct {
	mtvVersion    string
	buildTypes    []string             // ["prod", "stage"]
	ocpVersions   []string             // ["4.17", "4.18", "4.19"]
	iibData       map[string][]IIBInfo // key: "prod" or "stage", value: list of IIBInfo
	selectedBuild int                  // selected build type index (0=prod, 1=stage)
	selectedOCP   int                  // selected OCP version index
	focusedPane   int                  // 0=build types, 1=ocp versions, 2=details
	loading       bool
	spinner       spinner.Model
	table         table.Model // right pane details table
}

type ThemeSelectionModel struct {
	themes       []string // Available theme names
	selectedIdx  int      // Currently selected theme index
	currentTheme string   // Current active theme name
}

// Provider item for the providers list
type ProviderItem struct {
	name    string
	type_   string
	status  string // "connected", "error", "loading"
	vmCount int
}

func (i ProviderItem) FilterValue() string {
	return i.name + " " + i.type_ + " " + i.status
}
func (i ProviderItem) Title() string { return i.name }
func (i ProviderItem) Description() string {
	status := "❌ Error"
	switch i.status {
	case "connected":
		status = fmt.Sprintf("✅ %s (%d VMs)", i.type_, i.vmCount)
	case "loading":
		status = "⏳ Loading..."
	case "error":
		status = "❌ Error"
	}
	return status
}

// VM item for the VMs list
type VMItem struct {
	name        string
	provider    string
	status      string
	cpu         int32
	memoryGB    float64
	storageGB   float64
	guestOS     string
	ipAddresses []string
	networks    []string
	tags        []string
	isRunning   bool
}

func (i VMItem) FilterValue() string {
	searchText := i.name + " " + i.provider + " " + i.status + " " + i.guestOS
	for _, ip := range i.ipAddresses {
		searchText += " " + ip
	}
	for _, net := range i.networks {
		searchText += " " + net
	}
	for _, tag := range i.tags {
		searchText += " " + tag
	}
	return searchText
}
func (i VMItem) Title() string { return i.name }
func (i VMItem) Description() string {
	status := "❌ Stopped"
	if i.isRunning {
		status = fmt.Sprintf("✅ Running - %s", i.guestOS)
	}
	return status
}

// VM detail model
type VMDetailModel struct {
	vm      *VMInfo
	loading bool
	spinner spinner.Model
	table   table.Model
}

// Providers model for three-panel layout
type ProvidersModel struct {
	providers            []ProviderItem
	vms                  []VMItem
	loading              bool
	spinner              spinner.Model
	providersTable       table.Model               // Left pane: providers
	vmsTable             table.Model               // Middle pane: VMs
	detailView           VMDetailModel             // Right pane: VM details
	focusedPane          int                       // 0=providers, 1=vms, 2=details
	selectedProvider     int                       // Currently selected provider index
	selectedVM           int                       // Currently selected VM index
	showRunningOnly      bool                      // Filter to show only running VMs
	providerSearchInput  textinput.Model           // Search input for providers
	vmSearchInput        textinput.Model           // Search input for VMs
	searchingProviders   bool                      // Whether searching providers
	searchingVMs         bool                      // Whether searching VMs
	filteredProviderRows []table.Row               // Filtered provider rows
	filteredVMRows       []table.Row               // Filtered VM rows
	vmCache              map[string][]VMInfo       // Cache VMs by provider name
	providerConfigs      map[string]ProviderConfig // Provider configurations for credentials display
	vmScrollOffset       int                       // Scroll offset for VMs list
	maxVisibleVMs        int                       // Maximum VMs visible in panel
	expandedProvider     string                    // Name of currently expanded provider (for tree-like display)
	selectedTreeItem     int                       // Selected item within expanded tree (0=provider, 1=type, 2=url, 3=username, 4=password, 5=insecure)
	treeItemCount        int                       // Total number of items in expanded tree
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

// IIB-related messages
type IIBDataLoadedMsg struct {
	mtvVersion  string
	prodBuilds  []IIBInfo
	stageBuilds []IIBInfo
	err         error
}

// Provider-related messages
type ProvidersLoadedMsg struct {
	providers       []ProviderItem
	vmCache         map[string][]VMInfo       // Cache of VMs by provider name
	providerConfigs map[string]ProviderConfig // Provider configurations for immediate display
	err             error
}

type ProviderConnectionResult struct {
	name    string
	status  string
	vmCount int
	vms     []VMInfo
	err     error
}

type ProviderConnectionsCompletedMsg struct {
	connectionResults map[string]ProviderConnectionResult
	vmCache           map[string][]VMInfo
	err               error
}

type VMsLoadedMsg struct {
	providerName string
	vms          []VMInfo
	err          error
}

type VMDetailLoadedMsg struct {
	vm  *VMInfo
	err error
}

// Clipboard helper function variable for testing
var clipboardWriteAll = func(text string) error {
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
	mainMenuItems := []list.Item{
		MainMenuItem{
			title:       "Clusters",
			description: "Browse available clusters (press 'q' to quit)",
			action:      "list-clusters",
		},
		MainMenuItem{
			title:       "Providers",
			description: "Browse VM providers and virtual machines",
			action:      "providers",
		},
		MainMenuItem{
			title:       "IIB",
			description: "Retrieve Forklift FBC builds",
			action:      "iib-builds",
		},
		MainMenuItem{
			title:       "Themes",
			description: "Select UI theme",
			action:      "themes",
		},
	}

	// Create main menu list
	mainMenuList := list.New(mainMenuItems, MainMenuDelegate{}, 50, 14)
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

	// Style the cluster table using theme colors
	theme := GetCurrentTheme()
	tableStyles := table.DefaultStyles()
	tableStyles.Header = tableStyles.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(theme.Border).
		BorderBottom(true).
		Bold(true).
		Foreground(theme.Header)
	tableStyles.Selected = tableStyles.Selected.
		Foreground(theme.SelectionFg).
		Background(theme.Selection).
		Bold(false)
	clusterTable.SetStyles(tableStyles)

	// Setup spinner for loading
	s := spinner.New()
	s.Spinner = spinner.Dot
	s.Style = getSpinnerStyle()

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
	detailSpinner.Style = getSpinnerStyle()

	// Setup IIB input
	iibTextInput := textinput.New()
	iibTextInput.Placeholder = "Enter MTV version (e.g., 2.9)"
	iibTextInput.CharLimit = 10
	iibTextInput.Width = 30
	iibTextInput.Focus()

	// Setup IIB spinners
	iibInputSpinner := spinner.New()
	iibInputSpinner.Spinner = spinner.Dot
	iibInputSpinner.Style = getSpinnerStyle()

	iibDisplaySpinner := spinner.New()
	iibDisplaySpinner.Spinner = spinner.Dot
	iibDisplaySpinner.Style = getSpinnerStyle()

	// Setup IIB detail table
	iibDetailTable := table.New(
		table.WithColumns([]table.Column{
			{Title: "Field", Width: 15},
			{Title: "Value", Width: 50},
		}),
		table.WithRows([]table.Row{}),
		table.WithFocused(false),
	)
	iibDetailTable.SetStyles(tableStyles)

	// Setup providers components
	providersSpinner := spinner.New()
	providersSpinner.Spinner = spinner.Dot
	providersSpinner.Style = getSpinnerStyle()

	// Setup providers tables
	providersTable := table.New(
		table.WithColumns([]table.Column{
			{Title: "Provider", Width: 20},
			{Title: "Type", Width: 15},
			{Title: "Status", Width: 15},
		}),
		table.WithRows([]table.Row{}),
		table.WithFocused(true),
	)
	providersTable.SetStyles(tableStyles)

	vmsTable := table.New(
		table.WithColumns([]table.Column{
			{Title: "VM Name", Width: 25},
			{Title: "Status", Width: 15},
			{Title: "OS", Width: 20},
		}),
		table.WithRows([]table.Row{}),
		table.WithFocused(false),
	)
	vmsTable.SetStyles(tableStyles)

	vmDetailTable := table.New(
		table.WithColumns([]table.Column{
			{Title: "Field", Width: 15},
			{Title: "Value", Width: 50},
		}),
		table.WithRows([]table.Row{}),
		table.WithFocused(false),
	)
	vmDetailTable.SetStyles(tableStyles)

	// Setup provider search inputs
	providerSearchInput := textinput.New()
	providerSearchInput.Placeholder = "Search providers..."
	providerSearchInput.CharLimit = 50
	providerSearchInput.Width = 30

	vmSearchInput := textinput.New()
	vmSearchInput.Placeholder = "Search VMs..."
	vmSearchInput.CharLimit = 50
	vmSearchInput.Width = 30

	// Setup VM detail spinner
	vmDetailSpinner := spinner.New()
	vmDetailSpinner.Spinner = spinner.Dot
	vmDetailSpinner.Style = getSpinnerStyle()

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
		iibInput: IIBInputModel{
			textInput: iibTextInput,
			spinner:   iibInputSpinner,
		},
		iibDisplay: IIBDisplayModel{
			buildTypes:  []string{"prod", "stage"},
			ocpVersions: []string{"4.17", "4.18", "4.19"},
			iibData:     make(map[string][]IIBInfo),
			spinner:     iibDisplaySpinner,
			table:       iibDetailTable,
		},
		themeSelection: func() ThemeSelectionModel {
			themes := GetAvailableThemes()
			currentTheme := GetCurrentTheme().Name
			selectedIdx := 0
			// Find the index of the current theme
			for i, theme := range themes {
				if theme == currentTheme {
					selectedIdx = i
					break
				}
			}
			return ThemeSelectionModel{
				themes:       themes,
				selectedIdx:  selectedIdx,
				currentTheme: currentTheme,
			}
		}(),
		providers: ProvidersModel{
			providers:      []ProviderItem{},
			vms:            []VMItem{},
			loading:        true, // Start loading providers immediately
			spinner:        providersSpinner,
			providersTable: providersTable,
			vmsTable:       vmsTable,
			detailView: VMDetailModel{
				vm:      nil,
				loading: false,
				spinner: vmDetailSpinner,
				table:   vmDetailTable,
			},
			focusedPane:          0, // Start with providers pane focused
			selectedProvider:     0,
			selectedVM:           0,
			showRunningOnly:      false, // Show all VMs by default
			providerSearchInput:  providerSearchInput,
			vmSearchInput:        vmSearchInput,
			searchingProviders:   false,
			searchingVMs:         false,
			filteredProviderRows: []table.Row{},
			filteredVMRows:       []table.Row{},
			vmCache:              make(map[string][]VMInfo),
		},
		providerDetail: ProviderDetailModel{
			spinner: providersSpinner,
			table:   vmDetailTable, // Reuse the table styling
		},
		help: h,
		keys: keys,
	}
}

// Init initializes the model (required by tea.Model interface)
func (m AppModel) Init() tea.Cmd {
	// Pre-fetch clusters and providers in background for faster navigation
	var cmds []tea.Cmd

	// Start loading clusters
	m.clusterList.loading = true
	cmds = append(cmds, m.clusterList.spinner.Tick, m.loadClustersCmd())

	// Start loading providers (immediate configs, then async connections)
	m.providers.loading = true
	cmds = append(cmds, m.providers.spinner.Tick, m.loadProvidersCmd(), m.loadProviderConnectionsCmd())

	return tea.Batch(cmds...)
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
			// Global refresh - works on different screens
			switch m.screen {
			case MainMenuScreen:
				return m.refreshAllData()
			case ClusterListScreen:
				return m.refreshClusterList()
			case ProvidersScreen:
				return m.refreshProviders()
			default:
				// From any other screen, refresh all data in background
				return m.refreshAllData()
			}
		case "ctrl+u":
			// Single cluster refresh - only works on cluster list screen
			if m.screen == ClusterListScreen && !m.clusterList.loading && !m.clusterList.searching {
				return m.refreshSingleCluster()
			}
		case "/":
			// Activate search
			if m.screen == ClusterListScreen && !m.clusterList.loading {
				m.clusterList.searching = true
				m.clusterList.searchInput.Focus()
				return m, textinput.Blink
			}
			// Start search in VMs panel
			if m.screen == ProvidersScreen && !m.providers.loading && m.providers.focusedPane == 1 && len(m.providers.vms) > 0 {
				m.providers.searchingVMs = true
				m.providers.vmSearchInput.Focus()
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
				m.notification = "" // Clear notifications on back navigation
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
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			case IIBInputScreen:
				m.screen = MainMenuScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			case IIBDisplayScreen:
				m.screen = IIBInputScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			case ThemeSelectionScreen:
				m.screen = MainMenuScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			case ProvidersScreen:
				if m.providers.searchingVMs {
					// Exit VM search mode
					m.providers.searchingVMs = false
					m.providers.vmSearchInput.Blur()
					m.providers.vmSearchInput.SetValue("")
					return m, nil
				}
				m.screen = MainMenuScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			case ProviderDetailScreen:
				m.screen = m.previousScreen
				m.previousScreen = MainMenuScreen
				m.error = ""
				m.notification = "" // Clear notifications on back navigation
				return m, nil
			}

		case "r":
			// Refresh functionality
			if m.screen == ProvidersScreen {
				if m.providers.focusedPane == 0 && len(m.providers.providers) > 0 {
					// Refresh current provider's VMs
					selectedProvider := m.providers.providers[m.providers.selectedProvider]
					// Clear cache for this provider to force refresh
					delete(m.providers.vmCache, selectedProvider.name)
					return m, tea.Batch(
						m.loadVMsForProviderCmd(selectedProvider.name),
						showNotification(fmt.Sprintf("Refreshing %s...", selectedProvider.name), false),
					)
				}
			}
		case "pgup", "pageup":
			// Page up in VMs list
			if m.screen == ProvidersScreen && m.providers.focusedPane == 1 && len(m.providers.vms) > 0 {
				pageSize := m.providers.maxVisibleVMs
				if pageSize <= 0 {
					pageSize = 10
				}
				newSelection := m.providers.selectedVM - pageSize
				if newSelection < 0 {
					newSelection = 0
				}
				m.providers.selectedVM = newSelection
				m.providers.vmScrollOffset = newSelection
				// Update VM details
				selectedProviderName := m.providers.providers[m.providers.selectedProvider].name
				if cachedVMs, exists := m.providers.vmCache[selectedProviderName]; exists {
					if m.providers.selectedVM < len(cachedVMs) {
						m.providers.detailView.vm = &cachedVMs[m.providers.selectedVM]
					}
				}
				return m, nil
			}
		case "pgdn", "pagedown":
			// Page down in VMs list
			if m.screen == ProvidersScreen && m.providers.focusedPane == 1 && len(m.providers.vms) > 0 {
				pageSize := m.providers.maxVisibleVMs
				if pageSize <= 0 {
					pageSize = 10
				}
				newSelection := m.providers.selectedVM + pageSize
				if newSelection >= len(m.providers.vms) {
					newSelection = len(m.providers.vms) - 1
				}
				m.providers.selectedVM = newSelection
				if newSelection >= m.providers.vmScrollOffset+m.providers.maxVisibleVMs {
					m.providers.vmScrollOffset = newSelection - m.providers.maxVisibleVMs + 1
				}
				// Update VM details
				selectedProviderName := m.providers.providers[m.providers.selectedProvider].name
				if cachedVMs, exists := m.providers.vmCache[selectedProviderName]; exists {
					if m.providers.selectedVM < len(cachedVMs) {
						m.providers.detailView.vm = &cachedVMs[m.providers.selectedVM]
					}
				}
				return m, nil
			}
		case "tab":
			// Universal Tab navigation - move to next pane
			switch m.screen {
			case ClusterListScreen:
				if !m.clusterList.loading && !m.clusterList.searching {
					m.clusterList.focusedPane = 1 - m.clusterList.focusedPane // Toggle between 0 and 1
				}
			case IIBDisplayScreen:
				if !m.iibDisplay.loading {
					m.iibDisplay.focusedPane = (m.iibDisplay.focusedPane + 1) % 3 // Cycle through 0, 1, 2
				}
			case ProvidersScreen:
				m.providers.focusedPane = (m.providers.focusedPane + 1) % 3 // Cycle through 0, 1, 2
			}
			return m, nil
		case "shift+tab":
			// Universal Shift+Tab navigation - move to previous pane
			switch m.screen {
			case ClusterListScreen:
				if !m.clusterList.loading && !m.clusterList.searching {
					m.clusterList.focusedPane = 1 - m.clusterList.focusedPane // Toggle between 0 and 1
				}
			case IIBDisplayScreen:
				if !m.iibDisplay.loading {
					m.iibDisplay.focusedPane = (m.iibDisplay.focusedPane + 2) % 3 // Cycle backwards through 2, 1, 0
				}
			case ProvidersScreen:
				m.providers.focusedPane = (m.providers.focusedPane + 2) % 3 // Cycle backwards through 2, 1, 0
			}
			return m, nil
		case "up", "k":
			// Handle up navigation in IIB display screen
			if m.screen == IIBDisplayScreen && !m.iibDisplay.loading {
				switch m.iibDisplay.focusedPane {
				case 0: // Build types pane
					if m.iibDisplay.selectedBuild > 0 {
						m.iibDisplay.selectedBuild--
						m.updateOCPVersionsForSelectedBuildType()
					}
				case 1: // OCP versions pane
					if m.iibDisplay.selectedOCP > 0 {
						m.iibDisplay.selectedOCP--
					}
				}
				return m, nil
			}
			// Handle up navigation in provider detail screen
			if m.screen == ProviderDetailScreen {
				m.providerDetail.table.MoveUp(1)
				return m, nil
			}
			// Handle up navigation in providers screen
			if m.screen == ProvidersScreen && !m.providers.searchingVMs {
				switch m.providers.focusedPane {
				case 0: // Providers pane
					if len(m.providers.providers) > 0 {
						selectedProvider := m.providers.providers[m.providers.selectedProvider]

						// Check if we're in tree navigation mode (expanded provider AND tree item selected)
						if m.providers.expandedProvider == selectedProvider.name && m.providers.selectedTreeItem > 0 {
							// Navigate within the expanded tree (up)
							if m.providers.selectedTreeItem > 1 {
								m.providers.selectedTreeItem--
							}
							// Force re-render by returning model without any command
							return m, nil
						} else {
							// Normal provider navigation
							if m.providers.selectedProvider > 0 {
								m.providers.selectedProvider--
								// Reset tree selection when changing providers
								m.providers.selectedTreeItem = 0
								// Auto-load VMs for newly selected provider
								newSelectedProvider := m.providers.providers[m.providers.selectedProvider]
								return m, m.loadVMsForProviderWithCacheCmd(newSelectedProvider.name)
							}
						}
					}
				case 1: // VMs pane
					if len(m.providers.vms) > 0 && m.providers.selectedVM > 0 {
						m.providers.selectedVM--
						// Handle scrolling when selection goes above visible area
						if m.providers.selectedVM < m.providers.vmScrollOffset {
							m.providers.vmScrollOffset = m.providers.selectedVM
						}
						// Update details for newly selected VM using cached data
						selectedProviderName := m.providers.providers[m.providers.selectedProvider].name
						if cachedVMs, exists := m.providers.vmCache[selectedProviderName]; exists {
							if m.providers.selectedVM < len(cachedVMs) {
								m.providers.detailView.vm = &cachedVMs[m.providers.selectedVM]
							}
						}
					}
				}
				return m, nil
			}
		case "down", "j":
			// Handle down navigation in IIB display screen
			if m.screen == IIBDisplayScreen && !m.iibDisplay.loading {
				switch m.iibDisplay.focusedPane {
				case 0: // Build types pane
					if m.iibDisplay.selectedBuild < len(m.iibDisplay.buildTypes)-1 {
						m.iibDisplay.selectedBuild++
						m.updateOCPVersionsForSelectedBuildType()
					}
				case 1: // OCP versions pane
					if m.iibDisplay.selectedOCP < len(m.iibDisplay.ocpVersions)-1 {
						m.iibDisplay.selectedOCP++
					}
				}
				return m, nil
			}
			// Handle down navigation in provider detail screen
			if m.screen == ProviderDetailScreen {
				m.providerDetail.table.MoveDown(1)
				return m, nil
			}
			// Handle down navigation in providers screen
			if m.screen == ProvidersScreen && !m.providers.searchingVMs {
				switch m.providers.focusedPane {
				case 0: // Providers pane
					if len(m.providers.providers) > 0 {
						selectedProvider := m.providers.providers[m.providers.selectedProvider]

						// Check if we're in tree navigation mode (expanded provider AND tree item selected)
						if m.providers.expandedProvider == selectedProvider.name && m.providers.selectedTreeItem > 0 {
							// Navigate within the expanded tree (down) - tree has 5 items (1-5)
							if m.providers.selectedTreeItem < 5 {
								m.providers.selectedTreeItem++
							}
							// Force re-render by returning model without any command
							return m, nil
						} else {
							// Normal provider navigation
							if m.providers.selectedProvider < len(m.providers.providers)-1 {
								m.providers.selectedProvider++
								// Reset tree selection when changing providers
								m.providers.selectedTreeItem = 0
								// Auto-load VMs for newly selected provider
								newSelectedProvider := m.providers.providers[m.providers.selectedProvider]
								return m, m.loadVMsForProviderWithCacheCmd(newSelectedProvider.name)
							}
						}
					}
				case 1: // VMs pane
					if len(m.providers.vms) > 0 && m.providers.selectedVM < len(m.providers.vms)-1 {
						m.providers.selectedVM++
						// Handle scrolling when selection goes beyond visible area
						m.providers.maxVisibleVMs = (m.height - 15) / 2 // Rough calculation
						if m.providers.selectedVM >= m.providers.vmScrollOffset+m.providers.maxVisibleVMs {
							m.providers.vmScrollOffset = m.providers.selectedVM - m.providers.maxVisibleVMs + 1
						}
						// Update details for newly selected VM using cached data
						selectedProviderName := m.providers.providers[m.providers.selectedProvider].name
						if cachedVMs, exists := m.providers.vmCache[selectedProviderName]; exists {
							if m.providers.selectedVM < len(cachedVMs) {
								m.providers.detailView.vm = &cachedVMs[m.providers.selectedVM]
							}
						}
					}
				}
				return m, nil
			}
		case "left", "h":
			// Handle left navigation in providers screen - collapse tree
			if m.screen == ProvidersScreen {
				// If we're in provider pane and have an expanded tree, collapse it
				if m.providers.focusedPane == 0 && m.providers.expandedProvider != "" {
					m.providers.expandedProvider = ""
					m.providers.selectedTreeItem = 0
					return m, nil
				}
			}
		case "right", "l":
			// Handle right navigation in providers screen - expand tree
			if m.screen == ProvidersScreen {
				// If we're in provider pane and no tree is expanded, expand the selected provider
				if m.providers.focusedPane == 0 && m.providers.expandedProvider == "" && len(m.providers.providers) > 0 {
					selectedProvider := m.providers.providers[m.providers.selectedProvider]
					m.providers.expandedProvider = selectedProvider.name
					m.providers.selectedTreeItem = 1 // Start at first tree item (Type)
					return m, nil
				}
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
			case IIBInputScreen:
				// Submit MTV version
				version := strings.TrimSpace(m.iibInput.textInput.Value())
				if version != "" {
					m.iibInput.mtvVersion = version
					m.iibInput.loading = true
					m.iibInput.textInput.Blur()
					m.screen = IIBDisplayScreen
					m.iibDisplay.mtvVersion = version
					m.iibDisplay.loading = true
					m.iibDisplay.iibData = make(map[string][]IIBInfo)
					return m, tea.Batch(m.iibDisplay.spinner.Tick, m.loadIIBDataCmd(version))
				}
				return m, nil
			case IIBDisplayScreen:
				// Copy selected IIB to clipboard
				return m.handleIIBCopy()
			case ThemeSelectionScreen:
				// Apply selected theme
				return m.handleThemeSelection()
			case ProvidersScreen:
				// Handle selection in providers screen
				switch m.providers.focusedPane {
				case 0:
					// Provider pane: Handle tree navigation copying
					if len(m.providers.providers) > 0 {
						selectedProvider := m.providers.providers[m.providers.selectedProvider]

						// If provider is expanded and we're in tree navigation mode, copy selected item
						if m.providers.expandedProvider == selectedProvider.name && m.providers.selectedTreeItem > 0 {
							// Copy selected tree item to clipboard
							if config, exists := m.providers.providerConfigs[selectedProvider.name]; exists {
								var valueToCopy string

								switch m.providers.selectedTreeItem {
								case 1: // Type
									valueToCopy = config.Type
								case 2: // URL
									valueToCopy = config.URL
								case 3: // Username
									valueToCopy = config.Username
								case 4: // Password
									valueToCopy = config.Password
								case 5: // Insecure
									valueToCopy = fmt.Sprintf("%t", config.Insecure)
								}

								if valueToCopy != "" {
									err := clipboardWriteAll(valueToCopy)
									if err != nil {
										return m, showNotification("Failed to copy: "+err.Error(), true)
									}
									return m, showNotification("Copied to clipboard: "+valueToCopy, false)
								}
							}
						}
						// Note: Tree expansion/collapse is now handled by left/right arrows
						return m, nil
					}
				case 2:
					// VM Details pane: Copy VM name to clipboard
					if m.providers.detailView.vm != nil {
						vmName := m.providers.detailView.vm.Name
						err := clipboardWriteAll(vmName)
						if err != nil {
							return m, showNotification("Failed to copy VM name: "+err.Error(), true)
						}
						return m, showNotification("VM name copied to clipboard: "+vmName, false)
					}
				}
				return m, nil
			case ProviderDetailScreen:
				// Copy selected field to clipboard
				selectedRow := m.providerDetail.table.SelectedRow()
				if len(selectedRow) >= 2 {
					value := selectedRow[1] // Get the value column
					err := clipboardWriteAll(value)
					if err != nil {
						return m, showNotification("Failed to copy: "+err.Error(), true)
					}
					return m, showNotification("Copied to clipboard: "+value, false)
				}
				return m, nil
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

	case IIBDataLoadedMsg:
		m.iibInput.loading = false
		m.iibDisplay.loading = false

		if msg.err != nil {
			m.error = fmt.Sprintf("Failed to load IIB data: %v", msg.err)
			return m, nil
		}

		// Populate IIB data
		m.iibDisplay.iibData["prod"] = msg.prodBuilds
		m.iibDisplay.iibData["stage"] = msg.stageBuilds

		// Update OCP versions for currently selected build type
		m.updateOCPVersionsForSelectedBuildType()

		return m, showNotification(fmt.Sprintf("✅ IIB data loaded for MTV %s", msg.mtvVersion), false)

	case ProvidersLoadedMsg:
		// DON'T stop loading here - keep spinner going until connections complete
		// m.providers.loading = false // REMOVED - keep loading until ProviderConnectionsCompletedMsg
		if msg.err != nil {
			m.providers.loading = false // Only stop loading on error
			m.error = fmt.Sprintf("Failed to load providers: %v", msg.err)
			return m, nil
		}

		m.providers.providers = msg.providers
		m.providers.vmCache = msg.vmCache // Use the pre-cached VMs

		// Store provider configs for credentials display (use configs from message)
		m.providers.providerConfigs = msg.providerConfigs

		// Initialize VM scroll tracking
		m.providers.vmScrollOffset = 0
		m.providers.maxVisibleVMs = 10 // Will be calculated dynamically

		// Update provider table rows
		var rows []table.Row
		for _, provider := range msg.providers {
			statusDisplay := "❌ Error"
			switch provider.status {
			case "connected":
				statusDisplay = "✅ Connected"
			case "connecting":
				statusDisplay = "🟡 Connecting"
			case "error":
				statusDisplay = "❌ Error"
			}
			rows = append(rows, table.Row{provider.name, provider.type_, statusDisplay})
		}
		m.providers.providersTable.SetRows(rows)
		m.providers.filteredProviderRows = rows

		// Don't show final status here - let ProviderConnectionsCompletedMsg handle final status
		// Just return without notification to keep loading UI
		return m, nil

	case ProviderConnectionsCompletedMsg:
		m.providers.loading = false
		if msg.err != nil {
			m.error = fmt.Sprintf("Failed to connect to providers: %v", msg.err)
			return m, nil
		}

		// Update provider status based on connection results
		for i, provider := range m.providers.providers {
			if result, exists := msg.connectionResults[provider.name]; exists {
				m.providers.providers[i].status = result.status
				m.providers.providers[i].vmCount = result.vmCount
			}
		}

		// Store the VM cache from successful connections
		m.providers.vmCache = msg.vmCache

		// Store provider configs for credentials display (should already be done, but ensure)
		configs, _ := providerLoaderDeps.LoadProviderConfigs()
		m.providers.providerConfigs = configs

		// Update provider table rows with final status
		var rows []table.Row
		for _, provider := range m.providers.providers {
			statusDisplay := "❌ Error"
			switch provider.status {
			case "connected":
				statusDisplay = "✅ Connected"
			case "connecting":
				statusDisplay = "🟡 Connecting"
			case "error":
				statusDisplay = "❌ Error"
			}
			rows = append(rows, table.Row{provider.name, provider.type_, statusDisplay})
		}
		m.providers.providersTable.SetRows(rows)
		m.providers.filteredProviderRows = rows

		// Auto-load VMs for the first connected provider if we have cached data
		if len(m.providers.providers) > 0 {
			firstConnectedProvider := ""
			for _, provider := range m.providers.providers {
				if provider.status == "connected" {
					firstConnectedProvider = provider.name
					break
				}
			}

			if firstConnectedProvider != "" && len(msg.vmCache[firstConnectedProvider]) > 0 {
				// Convert VMInfo to VMItem for display
				cachedVMs := msg.vmCache[firstConnectedProvider]
				var vmItems []VMItem
				for _, vmInfo := range cachedVMs {
					vmItem := VMItem{
						name:        vmInfo.Name,
						provider:    firstConnectedProvider,
						status:      vmInfo.PowerState,
						cpu:         vmInfo.CPU,
						memoryGB:    float64(vmInfo.MemoryMB) / 1024,
						storageGB:   vmInfo.StorageGB,
						guestOS:     vmInfo.GuestOS,
						ipAddresses: vmInfo.IPAddresses,
						networks:    vmInfo.Networks,
						tags:        vmInfo.Tags,
						isRunning:   vmInfo.PowerState == "running" || vmInfo.PowerState == "poweredOn" || vmInfo.PowerState == "up" || vmInfo.PowerState == "ACTIVE",
					}
					vmItems = append(vmItems, vmItem)
				}

				m.providers.vms = vmItems
				m.providers.selectedVM = 0
				m.providers.vmScrollOffset = 0

				// Set first VM details
				if len(cachedVMs) > 0 {
					m.providers.detailView.vm = &cachedVMs[0]
				}

				// Update VM table rows
				var vmRows []table.Row
				for _, vm := range vmItems {
					statusDisplay := "❌ Stopped"
					if vm.isRunning {
						statusDisplay = "✅ Running"
					}
					vmRows = append(vmRows, table.Row{vm.name, statusDisplay, vm.guestOS})
				}
				m.providers.vmsTable.SetRows(vmRows)
				m.providers.filteredVMRows = vmRows
			}
		}

		// Count final connected/error providers
		connectedCount := 0
		errorCount := 0
		for _, provider := range m.providers.providers {
			switch provider.status {
			case "connected":
				connectedCount++
			case "error":
				errorCount++
			}
		}

		// Show final notification like clusters do
		totalProviders := len(m.providers.providers)
		if connectedCount > 0 && errorCount > 0 {
			return m, showNotification(fmt.Sprintf("✅ Provider loading complete: %d connected, %d failed", connectedCount, errorCount), false)
		} else if connectedCount > 0 {
			return m, showNotification(fmt.Sprintf("✅ Provider loading complete: %d connected", connectedCount), false)
		} else if totalProviders > 0 {
			return m, showNotification(fmt.Sprintf("❌ Provider loading complete: All %d providers failed", totalProviders), true)
		} else {
			return m, showNotification("⚠️ No providers found", true)
		}

	case VMsLoadedMsg:
		if msg.err != nil {
			// Update provider status to error
			for i, provider := range m.providers.providers {
				if provider.name == msg.providerName {
					m.providers.providers[i].status = "error"
					break
				}
			}
			return m, showNotification(fmt.Sprintf("Failed to load VMs: %v", msg.err), true)
		}

		// Update provider status to connected since VMs loaded successfully
		for i, provider := range m.providers.providers {
			if provider.name == msg.providerName {
				m.providers.providers[i].status = "connected"
				m.providers.providers[i].vmCount = len(msg.vms)
				break
			}
		}

		// Convert VMInfo to VMItem
		var vmItems []VMItem
		for _, vmInfo := range msg.vms {
			vmItem := VMItem{
				name:        vmInfo.Name,
				provider:    msg.providerName,
				status:      vmInfo.PowerState,
				cpu:         vmInfo.CPU,
				memoryGB:    float64(vmInfo.MemoryMB) / 1024,
				storageGB:   vmInfo.StorageGB,
				guestOS:     vmInfo.GuestOS,
				ipAddresses: vmInfo.IPAddresses,
				networks:    vmInfo.Networks,
				tags:        vmInfo.Tags,
				isRunning:   vmInfo.PowerState == "running" || vmInfo.PowerState == "poweredOn" || vmInfo.PowerState == "up" || vmInfo.PowerState == "ACTIVE",
			}
			vmItems = append(vmItems, vmItem)
		}

		m.providers.vms = vmItems
		// Cache VMs for this provider
		m.providers.vmCache[msg.providerName] = msg.vms

		// Reset VM selection and scroll offset
		m.providers.selectedVM = 0
		m.providers.vmScrollOffset = 0

		// Auto-load details for first VM if available
		if len(vmItems) > 0 {
			firstVM := msg.vms[0] // Use original VMInfo for details
			m.providers.detailView.vm = &firstVM
		} else {
			m.providers.detailView.vm = nil
		}

		// Update VM table rows
		var rows []table.Row
		for _, vm := range vmItems {
			statusDisplay := "❌ Stopped"
			if vm.isRunning {
				statusDisplay = "✅ Running"
			}
			rows = append(rows, table.Row{vm.name, statusDisplay, vm.guestOS})
		}
		m.providers.vmsTable.SetRows(rows)
		m.providers.filteredVMRows = rows

		return m, showNotification(fmt.Sprintf("✅ Loaded %d VMs from %s", len(vmItems), msg.providerName), false)

	case VMDetailLoadedMsg:
		m.providers.detailView.loading = false
		if msg.err != nil {
			return m, showNotification(fmt.Sprintf("Failed to load VM details: %v", msg.err), true)
		}

		m.providers.detailView.vm = msg.vm
		return m, nil
	}

	// Handle screen-specific updates
	switch m.screen {
	case MainMenuScreen:
		m.mainMenu.list, cmd = m.mainMenu.list.Update(msg)
		// Update spinners for any background loading operations
		var spinnerCmds []tea.Cmd
		if m.clusterList.loading {
			var spinnerCmd tea.Cmd
			m.clusterList.spinner, spinnerCmd = m.clusterList.spinner.Update(msg)
			if spinnerCmd != nil {
				spinnerCmds = append(spinnerCmds, spinnerCmd)
			}
		}
		if m.providers.loading {
			var spinnerCmd tea.Cmd
			m.providers.spinner, spinnerCmd = m.providers.spinner.Update(msg)
			if spinnerCmd != nil {
				spinnerCmds = append(spinnerCmds, spinnerCmd)
			}
		}
		// Batch all spinner commands with the main command
		if len(spinnerCmds) > 0 {
			if cmd != nil {
				cmd = tea.Batch(append([]tea.Cmd{cmd}, spinnerCmds...)...)
			} else {
				cmd = tea.Batch(spinnerCmds...)
			}
		}
	case ClusterDetailScreen:
		if !m.clusterDetail.loading {
			m.clusterDetail.table, cmd = m.clusterDetail.table.Update(msg)
		} else {
			m.clusterDetail.spinner, cmd = m.clusterDetail.spinner.Update(msg)
		}
	case ClusterListScreen:
		// Handle both cluster and provider spinners when on cluster list screen
		var spinnerCmds []tea.Cmd
		if m.clusterList.loading {
			var spinnerCmd tea.Cmd
			m.clusterList.spinner, spinnerCmd = m.clusterList.spinner.Update(msg)
			if spinnerCmd != nil {
				spinnerCmds = append(spinnerCmds, spinnerCmd)
			}
		}
		// Also update provider spinner if providers are loading in background
		if m.providers.loading {
			var spinnerCmd tea.Cmd
			m.providers.spinner, spinnerCmd = m.providers.spinner.Update(msg)
			if spinnerCmd != nil {
				spinnerCmds = append(spinnerCmds, spinnerCmd)
			}
		}

		if m.clusterList.loading {
			// If cluster loading, just handle spinners
			if len(spinnerCmds) > 0 {
				cmd = tea.Batch(spinnerCmds...)
			}
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

		// Always batch any background spinner commands
		if len(spinnerCmds) > 0 {
			if cmd != nil {
				cmd = tea.Batch(append([]tea.Cmd{cmd}, spinnerCmds...)...)
			} else {
				cmd = tea.Batch(spinnerCmds...)
			}
		}
	case IIBInputScreen:
		if !m.iibInput.loading {
			m.iibInput.textInput, cmd = m.iibInput.textInput.Update(msg)
		} else {
			m.iibInput.spinner, cmd = m.iibInput.spinner.Update(msg)
		}
	case IIBDisplayScreen:
		// Navigation is handled in the main keyboard section, only handle spinner here
		if m.iibDisplay.loading {
			m.iibDisplay.spinner, cmd = m.iibDisplay.spinner.Update(msg)
		}
	case ThemeSelectionScreen:
		// Handle navigation in theme selection
		if keyMsg, ok := msg.(tea.KeyMsg); ok {
			switch keyMsg.String() {
			case "up", "k":
				if m.themeSelection.selectedIdx > 0 {
					m.themeSelection.selectedIdx--
				}
			case "down", "j":
				if m.themeSelection.selectedIdx < len(m.themeSelection.themes)-1 {
					m.themeSelection.selectedIdx++
				}
			}
		}
	case ProvidersScreen:
		// Handle spinner updates when loading, but also allow navigation
		if m.providers.loading {
			m.providers.spinner, cmd = m.providers.spinner.Update(msg)
			// Don't return here - allow navigation even during loading
		}

		if m.providers.searchingVMs {
			// Handle VM search input
			var searchCmd tea.Cmd
			m.providers.vmSearchInput, searchCmd = m.providers.vmSearchInput.Update(msg)

			// Filter VMs based on search input
			query := m.providers.vmSearchInput.Value()
			if query != "" {
				var filteredVMs []VMItem
				for _, vm := range m.providers.vms {
					if strings.Contains(strings.ToLower(vm.name), strings.ToLower(query)) ||
						strings.Contains(strings.ToLower(vm.guestOS), strings.ToLower(query)) {
						filteredVMs = append(filteredVMs, vm)
					}
				}
				// Reset selection to first filtered item
				if len(filteredVMs) > 0 {
					m.providers.selectedVM = 0
					m.providers.vmScrollOffset = 0
					// Update details for first filtered VM
					selectedProviderName := m.providers.providers[m.providers.selectedProvider].name
					if cachedVMs, exists := m.providers.vmCache[selectedProviderName]; exists {
						// Find the VM in the cache that matches the first filtered VM
						for _, cachedVM := range cachedVMs {
							if cachedVM.Name == filteredVMs[0].name {
								m.providers.detailView.vm = &cachedVM
								break
							}
						}
					}
				}
			}

			// Handle navigation keys in search mode
			if msg, ok := msg.(tea.KeyMsg); ok {
				switch msg.String() {
				case "up", "down":
					// Let normal navigation handle these
					return m, nil
				case "enter":
					// Exit search mode and handle normal selection
					m.providers.searchingVMs = false
					m.providers.vmSearchInput.Blur()
					return m, nil
				default:
					cmd = searchCmd
				}
			} else {
				cmd = searchCmd
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
	case "iib-builds":
		m.previousScreen = MainMenuScreen
		m.screen = IIBInputScreen
		// Reset IIB input state
		m.iibInput.textInput.SetValue("")
		m.iibInput.mtvVersion = ""
		m.iibInput.loading = false
		m.iibInput.textInput.Focus()
		return m, textinput.Blink
	case "providers":
		m.previousScreen = MainMenuScreen
		m.screen = ProvidersScreen
		// Data should already be pre-fetched, just continue spinner if still loading
		if m.providers.loading {
			return m, m.providers.spinner.Tick
		}
		return m, nil
	case "themes":
		m.previousScreen = MainMenuScreen
		m.screen = ThemeSelectionScreen
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

// Refresh all data - clusters and providers
func (m AppModel) refreshAllData() (AppModel, tea.Cmd) {
	var cmds []tea.Cmd

	// Refresh clusters
	m.clusterList.clusterInfo = make(map[string]*ClusterInfo)
	m.clusterList.clusterPasswords = make(map[string]string)
	m.clusterList.clusters = []ClusterItem{}
	m.clusterList.list.SetItems([]list.Item{})
	m.clusterList.table.SetRows([]table.Row{})
	m.clusterList.filteredRows = []table.Row{}
	m.clusterList.loading = true
	m.clusterList.searching = false
	m.clusterList.searchInput.SetValue("")
	m.clusterList.searchInput.Blur()
	cmds = append(cmds, m.clusterList.spinner.Tick, m.loadClustersCmd())

	// Refresh providers
	m.providers.providers = []ProviderItem{}
	m.providers.vms = []VMItem{}
	m.providers.vmCache = make(map[string][]VMInfo)
	m.providers.loading = true
	m.providers.searchingVMs = false
	m.providers.vmSearchInput.SetValue("")
	m.providers.vmSearchInput.Blur()
	cmds = append(cmds, m.providers.spinner.Tick, m.loadProvidersCmd(), m.loadProviderConnectionsCmd())

	m.error = "" // Clear any previous errors

	return m, tea.Batch(append(cmds, showNotification("Refreshing all data...", false))...)
}

// Refresh providers only
func (m AppModel) refreshProviders() (AppModel, tea.Cmd) {
	// Clear provider data and reload
	m.providers.providers = []ProviderItem{}
	m.providers.vms = []VMItem{}
	m.providers.vmCache = make(map[string][]VMInfo)
	m.providers.loading = true
	m.providers.searchingVMs = false
	m.providers.vmSearchInput.SetValue("")
	m.providers.vmSearchInput.Blur()
	m.error = ""

	return m, tea.Batch(
		m.providers.spinner.Tick,
		m.loadProvidersCmd(),
		m.loadProviderConnectionsCmd(),
		showNotification("Refreshing providers...", false),
	)
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
	case IIBInputScreen:
		mainContent = m.renderIIBInput()
	case IIBDisplayScreen:
		mainContent = m.renderIIBDisplay()
	case ThemeSelectionScreen:
		mainContent = m.renderThemeSelection()
	case ProvidersScreen:
		mainContent = m.renderProviders()
	case ProviderDetailScreen:
		mainContent = m.renderProviderDetail()
	default:
		mainContent = "Unknown screen"
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

	// Menu items - use actual menu items from the list
	menuItems := m.mainMenu.list.Items()
	selectedIndex := m.mainMenu.list.Index()

	for i, item := range menuItems {
		menuItem := item.(MainMenuItem)
		var styledItem string
		if i == selectedIndex {
			styledItem = selectedItemStyle.Render(menuItem.title)
		} else {
			styledItem = menuItemStyle.Render(menuItem.title)
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

	// Show separate loading indicators for each service
	if m.clusterList.loading {
		var loadingMessage string

		if len(m.clusterList.list.Items()) == 0 {
			loadingMessage = "Loading clusters in background..."
		} else {
			loadingMessage = "Refreshing clusters..."
		}

		loadingIndicator := StatusLoading(loadingMessage)
		centeredLoading := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(loadingIndicator)
		content.WriteString(centeredLoading + "\n")

		spinnerView := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(m.clusterList.spinner.View())
		content.WriteString(spinnerView + "\n")

		if m.providers.loading {
			content.WriteString("\n") // Extra space between loading indicators
		}
	}

	if m.providers.loading {
		loadingMessage := "Loading providers in background..."
		loadingIndicator := StatusLoading(loadingMessage)
		centeredLoading := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(loadingIndicator)
		content.WriteString(centeredLoading + "\n")

		spinnerView := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(m.providers.spinner.View())
		content.WriteString(spinnerView + "\n")
	}

	// Show status of completed operations independently
	hasStatus := false

	// Show cluster status if clusters are done loading
	if !m.clusterList.loading {
		if len(m.clusterList.list.Items()) > 0 {
			clusterCount := len(m.clusterList.list.Items())
			readyIndicator := fmt.Sprintf("✅ %d clusters ready", clusterCount)
			centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(readyIndicator)
			content.WriteString(centeredIndicator + "\n")
			hasStatus = true
		} else {
			// Clusters finished loading but none found
			readyIndicator := "⚠️ No clusters found"
			centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(readyIndicator)
			content.WriteString(centeredIndicator + "\n")
			hasStatus = true
		}
	}

	// Show provider status if providers are done loading
	if !m.providers.loading {
		// Count actually connected providers
		connectedCount := 0
		errorCount := 0
		for _, provider := range m.providers.providers {
			switch provider.status {
			case "connected":
				connectedCount++
			case "error":
				errorCount++
			}
		}

		if connectedCount > 0 {
			if errorCount > 0 {
				statusIndicator := fmt.Sprintf("⚠️  %d providers connected, %d failed", connectedCount, errorCount)
				centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(statusIndicator)
				content.WriteString(centeredIndicator + "\n")
			} else {
				readyIndicator := fmt.Sprintf("✅ %d providers connected", connectedCount)
				centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(readyIndicator)
				content.WriteString(centeredIndicator + "\n")
			}
			hasStatus = true
		} else if len(m.providers.providers) > 0 {
			errorIndicator := fmt.Sprintf("❌ All %d providers failed to connect", len(m.providers.providers))
			centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(errorIndicator)
			content.WriteString(centeredIndicator + "\n")
			hasStatus = true
		} else {
			// Providers finished loading but none found (likely an error)
			readyIndicator := "⚠️ No providers loaded"
			if m.error != "" && strings.Contains(strings.ToLower(m.error), "provider") {
				readyIndicator = "❌ Provider loading failed"
			}
			centeredIndicator := lipgloss.NewStyle().Width(m.width).Align(lipgloss.Center).Render(readyIndicator)
			content.WriteString(centeredIndicator + "\n")
			hasStatus = true
		}
	}

	// Add consolidated instructions if any data is loaded
	if hasStatus {
		content.WriteString("\n")
		instructions := "💡 Press Enter to navigate • Ctrl+R to refresh all data"
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

	// Fixed height for all panels to prevent jumping
	panelHeight := m.height - 10 // Reserve space for title and instructions
	if panelHeight < 15 {
		panelHeight = 15 // Minimum height
	}

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

	// Style table using theme colors
	theme := GetCurrentTheme()
	tableStyles := table.DefaultStyles()
	tableStyles.Header = tableStyles.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(theme.Border).
		BorderBottom(true).
		Bold(true).
		Foreground(theme.Header)
	tableStyles.Selected = tableStyles.Selected.
		Foreground(theme.SelectionFg).
		Background(theme.Selection).
		Bold(false)
	leftTable.SetStyles(tableStyles)

	leftContent.WriteString(leftTable.View())

	// RIGHT PANE: Simple cluster details
	rightContent := m.renderSimpleClusterDetails(rightWidth - 6) // Account for border and padding

	// Create bordered panes with FIXED HEIGHT
	leftPane := lipgloss.NewStyle().
		Width(leftWidth).
		Height(panelHeight).
		Border(lipgloss.RoundedBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(0, 1).
		Render(leftContent.String())

	rightPane := lipgloss.NewStyle().
		Width(rightWidth).
		Height(panelHeight).
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

	// Style the table using theme colors
	theme := GetCurrentTheme()
	s := table.DefaultStyles()
	s.Header = s.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(theme.Border).
		BorderBottom(true).
		Bold(false).
		Foreground(theme.Header)
	s.Selected = s.Selected.
		Foreground(theme.SelectionFg).
		Background(theme.Selection).
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

	// Style the table using theme colors
	theme := GetCurrentTheme()
	s := table.DefaultStyles()
	s.Header = s.Header.
		BorderStyle(lipgloss.NormalBorder()).
		BorderForeground(theme.Border).
		BorderBottom(true).
		Bold(false).
		Foreground(theme.Header)
	s.Selected = s.Selected.
		Foreground(theme.SelectionFg).
		Background(theme.Selection).
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

// Render IIB input screen
func (m AppModel) renderIIBInput() string {
	var content strings.Builder

	// Title
	title := "IIB - Enter MTV Version"
	centeredTitle := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render(title)
	content.WriteString(centeredTitle + "\n\n\n")

	if m.iibInput.loading {
		// Show loading state
		loadingText := "🔍 Loading IIB data for MTV " + m.iibInput.mtvVersion + "..."
		centeredLoading := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(loadingText)
		content.WriteString(centeredLoading + "\n\n")

		spinnerView := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(m.iibInput.spinner.View())
		content.WriteString(spinnerView)
	} else {
		// Show input field
		inputLabel := "MTV Version:"
		centeredLabel := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(inputLabel)
		content.WriteString(centeredLabel + "\n\n")

		centeredInput := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(m.iibInput.textInput.View())
		content.WriteString(centeredInput + "\n\n\n")

		// Instructions
		instructions := "💡 Enter MTV version (e.g., 2.9) and press Enter • Esc to go back"
		centeredInstructions := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Foreground(lipgloss.Color("240")).
			Render(instructions)
		content.WriteString(centeredInstructions)
	}

	return content.String()
}

// Render IIB display screen with three panels
func (m AppModel) renderIIBDisplay() string {
	if m.iibDisplay.loading {
		var content strings.Builder

		loadingText := "🔍 Loading IIB data for MTV " + m.iibDisplay.mtvVersion + "..."
		centeredLoading := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(loadingText)
		content.WriteString(centeredLoading + "\n\n")

		spinnerView := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(m.iibDisplay.spinner.View())
		content.WriteString(spinnerView)

		return content.String()
	}

	// Calculate fixed dimensions for stable layout
	totalWidth := m.width - 4
	buildWidth := totalWidth * 25 / 100
	ocpWidth := totalWidth * 25 / 100
	detailWidth := totalWidth - buildWidth - ocpWidth

	// Fixed height for all panels to prevent jumping
	panelHeight := m.height - 10 // Reserve space for title and instructions
	if panelHeight < 15 {
		panelHeight = 15 // Minimum height
	}

	// Calculate available lines for content (subtract title + padding)
	availableLines := panelHeight - 6 // Title, padding, borders

	// Build left panel (Build Types) with fixed content area
	var leftContent strings.Builder
	leftTitle := "Build Types"
	if m.iibDisplay.focusedPane == 0 {
		leftTitle = "🎯 " + leftTitle
	}
	leftContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(leftTitle) + "\n\n")

	for i, buildType := range m.iibDisplay.buildTypes {
		if i >= availableLines-2 {
			leftContent.WriteString("... (" + fmt.Sprintf("%d more", len(m.iibDisplay.buildTypes)-i) + ")\n")
			break
		}

		var icon, display string
		if buildType == "prod" {
			icon = "🟢"
			display = "Production"
		} else {
			icon = "🟡"
			display = "Stage"
		}

		item := icon + " " + display
		if i == m.iibDisplay.selectedBuild {
			item = selectedItemStyle.Render(item)
		} else {
			item = menuItemStyle.Render(item)
		}
		leftContent.WriteString(item + "\n")
	}

	// Fill remaining space to maintain consistent height
	currentLines := strings.Count(leftContent.String(), "\n")
	for currentLines < panelHeight-4 {
		leftContent.WriteString("\n")
		currentLines++
	}

	// Build middle panel (OCP Versions) with fixed content area
	var middleContent strings.Builder
	middleTitle := "OCP Versions"
	if m.iibDisplay.focusedPane == 1 {
		middleTitle = "🎯 " + middleTitle
	}
	middleContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(middleTitle) + "\n\n")

	for i, version := range m.iibDisplay.ocpVersions {
		if i >= availableLines-2 {
			middleContent.WriteString("... (" + fmt.Sprintf("%d more", len(m.iibDisplay.ocpVersions)-i) + ")\n")
			break
		}

		item := "📋 " + version
		if i == m.iibDisplay.selectedOCP {
			item = selectedItemStyle.Render(item)
		} else {
			item = menuItemStyle.Render(item)
		}
		middleContent.WriteString(item + "\n")
	}

	// Fill remaining space to maintain consistent height
	currentLines = strings.Count(middleContent.String(), "\n")
	for currentLines < panelHeight-4 {
		middleContent.WriteString("\n")
		currentLines++
	}

	// Build right panel (Details) with fixed content area
	var rightContent strings.Builder
	rightTitle := "IIB Details"
	if m.iibDisplay.focusedPane == 2 {
		rightTitle = "🎯 " + rightTitle
	}
	rightContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(rightTitle) + "\n\n")

	// Get selected build data
	selectedBuildType := m.iibDisplay.buildTypes[m.iibDisplay.selectedBuild]
	selectedOCPVersion := m.iibDisplay.ocpVersions[m.iibDisplay.selectedOCP]

	if builds, exists := m.iibDisplay.iibData[selectedBuildType]; exists {
		// Find build for selected OCP version
		var selectedBuild *IIBInfo
		for _, build := range builds {
			if build.OCPVersion == selectedOCPVersion {
				selectedBuild = &build
				break
			}
		}

		if selectedBuild != nil {
			rightContent.WriteString(Field("MTV Version", selectedBuild.MTVVersion) + "\n")
			rightContent.WriteString(Field("IIB", selectedBuild.IIB) + "\n")
			rightContent.WriteString(Field("OCP Version", selectedBuild.OCPVersion) + "\n")
			rightContent.WriteString(Field("Created", selectedBuild.Created) + "\n")
			rightContent.WriteString(Field("Environment", selectedBuild.Environment) + "\n\n")
			rightContent.WriteString("💡 Press Enter to copy IIB to clipboard")
		} else {
			rightContent.WriteString("No build available for " + selectedOCPVersion)
		}
	} else {
		rightContent.WriteString("No builds available")
	}

	// Fill remaining space to maintain consistent height
	currentLines = strings.Count(rightContent.String(), "\n")
	for currentLines < panelHeight-4 {
		rightContent.WriteString("\n")
		currentLines++
	}

	// Combine panels with borders and FIXED HEIGHT
	leftPanel := lipgloss.NewStyle().
		Width(buildWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(leftContent.String())

	middlePanel := lipgloss.NewStyle().
		Width(ocpWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(middleContent.String())

	rightPanel := lipgloss.NewStyle().
		Width(detailWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(rightContent.String())

	// Combine panels horizontally
	mainContent := lipgloss.JoinHorizontal(lipgloss.Top, leftPanel, middlePanel, rightPanel)

	// Add title above panels
	var content strings.Builder
	title := fmt.Sprintf("=== MTV %s Forklift FBC Builds ===", m.iibDisplay.mtvVersion)
	centeredTitle := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render(title)
	content.WriteString(centeredTitle + "\n\n")

	content.WriteString(mainContent)

	// Add navigation instructions
	content.WriteString("\n\n")
	instructions := "Navigation: ↑/↓/j/k Navigate, Tab/Shift+Tab Switch Panel, Enter Copy, Esc Back"
	centeredInstructions := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Foreground(lipgloss.Color("240")).
		Render(instructions)
	content.WriteString(centeredInstructions)

	return content.String()
}

// Load IIB data command
func (m AppModel) loadIIBDataCmd(mtvVersion string) tea.Cmd {
	return func() tea.Msg {
		// Check if already logged into kuflox
		if !iibLoaderDeps.CheckKufloxLogin() {
			// Need to login first
			if err := iibLoaderDeps.LoginToKuflox(); err != nil {
				return IIBDataLoadedMsg{
					mtvVersion: mtvVersion,
					err:        fmt.Errorf("failed to login to kuflox cluster: %w", err),
				}
			}
		}

		// Get production builds
		prodBuilds, err := iibLoaderDeps.GetForkliftBuilds("prod")
		if err != nil {
			return IIBDataLoadedMsg{
				mtvVersion: mtvVersion,
				err:        fmt.Errorf("failed to get production builds: %w", err),
			}
		}

		// Get stage builds
		stageBuilds, err := iibLoaderDeps.GetForkliftBuilds("stage")
		if err != nil {
			return IIBDataLoadedMsg{
				mtvVersion: mtvVersion,
				err:        fmt.Errorf("failed to get stage builds: %w", err),
			}
		}

		return IIBDataLoadedMsg{
			mtvVersion:  mtvVersion,
			prodBuilds:  prodBuilds,
			stageBuilds: stageBuilds,
		}
	}
}

// Update OCP versions based on currently selected build type
func (m *AppModel) updateOCPVersionsForSelectedBuildType() {
	selectedBuildType := m.iibDisplay.buildTypes[m.iibDisplay.selectedBuild]

	// Get builds for the selected build type
	builds, exists := m.iibDisplay.iibData[selectedBuildType]
	if !exists {
		m.iibDisplay.ocpVersions = []string{}
		m.iibDisplay.selectedOCP = 0
		return
	}

	// Extract OCP versions for this build type only
	ocpVersionsSet := make(map[string]bool)
	for _, build := range builds {
		ocpVersionsSet[build.OCPVersion] = true
	}

	// Convert set to sorted slice
	var availableVersions []string
	for version := range ocpVersionsSet {
		availableVersions = append(availableVersions, version)
	}
	sort.Strings(availableVersions)
	m.iibDisplay.ocpVersions = availableVersions

	// Reset selected index if out of bounds
	if m.iibDisplay.selectedOCP >= len(availableVersions) {
		m.iibDisplay.selectedOCP = 0
	}
}

// Handle IIB copy to clipboard
func (m AppModel) handleIIBCopy() (AppModel, tea.Cmd) {
	// Get selected build data
	selectedBuildType := m.iibDisplay.buildTypes[m.iibDisplay.selectedBuild]
	selectedOCPVersion := m.iibDisplay.ocpVersions[m.iibDisplay.selectedOCP]

	if builds, exists := m.iibDisplay.iibData[selectedBuildType]; exists {
		// Find build for selected OCP version
		for _, build := range builds {
			if build.OCPVersion == selectedOCPVersion {
				// Copy IIB string to clipboard
				err := clipboardWriteAll(build.IIB)
				if err != nil {
					return m, showNotification("Failed to copy to clipboard: "+err.Error(), true)
				}
				return m, showNotification("IIB copied to clipboard: "+build.IIB, false)
			}
		}
	}

	return m, showNotification("No IIB data to copy", true)
}

// Handle theme selection and apply the new theme
func (m AppModel) handleThemeSelection() (AppModel, tea.Cmd) {
	selectedThemeName := m.themeSelection.themes[m.themeSelection.selectedIdx]

	// Don't change if it's already the current theme
	if selectedThemeName == m.themeSelection.currentTheme {
		return m, showNotification("Theme is already active", false)
	}

	// Apply the new theme
	newTheme := GetThemeByName(selectedThemeName)
	if newTheme != nil {
		SetTheme(*newTheme)
		UpdateStyles()
		m.themeSelection.currentTheme = selectedThemeName
		return m, showNotification(fmt.Sprintf("Applied %s theme", selectedThemeName), false)
	}

	return m, showNotification("Failed to apply theme", true)
}

// Render theme selection screen
func (m AppModel) renderThemeSelection() string {
	var content strings.Builder

	// Theme selection title - centered
	title := "Theme Selection"
	centeredTitle := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render(title)
	content.WriteString(centeredTitle + "\n\n\n")

	// Current theme indicator
	currentThemeText := fmt.Sprintf("Current: %s", m.themeSelection.currentTheme)
	centeredCurrent := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Foreground(lipgloss.Color("33")).
		Render(currentThemeText)
	content.WriteString(centeredCurrent + "\n\n")

	// Theme items - manually centered
	for i, themeName := range m.themeSelection.themes {
		var indicator string
		if themeName == m.themeSelection.currentTheme {
			indicator = "✅ "
		} else {
			indicator = "   "
		}

		var styledItem string
		itemText := indicator + themeName
		if i == m.themeSelection.selectedIdx {
			styledItem = selectedItemStyle.Render(itemText)
		} else {
			styledItem = menuItemStyle.Render(itemText)
		}

		// Center each theme item
		centeredItem := lipgloss.NewStyle().
			Width(m.width).
			Align(lipgloss.Center).
			Render(styledItem)
		content.WriteString(centeredItem + "\n")
	}

	// Add instructions
	content.WriteString("\n\n")
	instructions := "💡 Use ↑↓ to navigate • Enter to apply theme • Esc to go back"
	centeredInstructions := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Foreground(lipgloss.Color("240")).
		Render(instructions)
	content.WriteString(centeredInstructions)

	return content.String()
}

// Command to load providers - immediate config loading, then async connections
func (m AppModel) loadProvidersCmd() tea.Cmd {
	return func() tea.Msg {
		// Load provider configurations from Python config file (immediate)
		providerConfigs, err := providerLoaderDeps.LoadProviderConfigs()
		if err != nil {
			return ProvidersLoadedMsg{
				providers:       []ProviderItem{},
				providerConfigs: make(map[string]ProviderConfig),
				err:             fmt.Errorf("failed to load provider configurations: %w", err),
			}
		}

		// Create initial provider items with "connecting" status (immediate display)
		var providers []ProviderItem
		supportedProviderConfigs := make(map[string]ProviderConfig)

		for name, config := range providerConfigs {
			if config.Type == "vmware" || config.Type == "ovirt" || config.Type == "openstack" {
				// Add provider immediately with connecting status
				providers = append(providers, ProviderItem{
					name:    name,
					type_:   config.Type,
					status:  "connecting",
					vmCount: 0,
				})

				// Store config for immediate detail screen access
				supportedProviderConfigs[name] = config
			}
		}

		if len(providers) == 0 {
			return ProvidersLoadedMsg{
				providers:       []ProviderItem{},
				providerConfigs: make(map[string]ProviderConfig),
				err:             fmt.Errorf("no supported providers found (vmware, ovirt, openstack)"),
			}
		}

		// Sort providers by name for consistent display
		sort.Slice(providers, func(i, j int) bool {
			return providers[i].name < providers[j].name
		})

		// Return immediate result with "connecting" providers, async connections will update status
		return ProvidersLoadedMsg{
			providers:       providers,
			vmCache:         make(map[string][]VMInfo),
			providerConfigs: supportedProviderConfigs, // Include configs for immediate detail screen access
			err:             nil,
		}
	}
}

// Command to load provider connections asynchronously (called after initial load)
func (m AppModel) loadProviderConnectionsCmd() tea.Cmd {
	return func() tea.Msg {
		// Load provider configurations again (should be cached/fast)
		providerConfigs, err := providerLoaderDeps.LoadProviderConfigs()
		if err != nil {
			return ProviderConnectionsCompletedMsg{
				err: fmt.Errorf("failed to load provider configurations: %w", err),
			}
		}

		// Filter supported provider types
		var supportedConfigs []struct {
			name   string
			config ProviderConfig
		}
		for name, config := range providerConfigs {
			if config.Type == "vmware" || config.Type == "ovirt" || config.Type == "openstack" {
				supportedConfigs = append(supportedConfigs, struct {
					name   string
					config ProviderConfig
				}{name, config})
			}
		}

		if len(supportedConfigs) == 0 {
			return ProviderConnectionsCompletedMsg{
				err: fmt.Errorf("no supported providers found"),
			}
		}

		// Concurrent provider connection attempts
		resultChan := make(chan ProviderConnectionResult, len(supportedConfigs))
		vmCache := make(map[string][]VMInfo)

		// Launch goroutine for each provider
		for _, providerConfig := range supportedConfigs {
			go func(name string, config ProviderConfig) {
				defer func() {
					if r := recover(); r != nil {
						resultChan <- ProviderConnectionResult{
							name:   name,
							status: "error",
							err:    fmt.Errorf("panic in %s: %v", name, r),
						}
					}
				}()

				// Try to create and connect to provider
				provider, err := providerLoaderDeps.CreateProvider(
					config.Type,
					config.URL,
					config.Username,
					config.Password,
					config.Insecure,
					config.ExtraParams,
				)
				if err != nil {
					resultChan <- ProviderConnectionResult{
						name:   name,
						status: "error",
						err:    fmt.Errorf("failed to create provider %s: %w", name, err),
					}
					return
				}

				// Connect to the provider
				if err := provider.Connect(); err != nil {
					resultChan <- ProviderConnectionResult{
						name:   name,
						status: "error",
						err:    fmt.Errorf("failed to connect to provider %s: %w", name, err),
					}
					return
				}

				// List VMs
				ctx := context.Background()
				vms, err := provider.ListVMs(ctx)
				_ = provider.Close() // Always close the connection

				if err != nil {
					resultChan <- ProviderConnectionResult{
						name:   name,
						status: "error",
						err:    fmt.Errorf("failed to list VMs for %s: %w", name, err),
					}
					return
				}

				// Success!
				resultChan <- ProviderConnectionResult{
					name:    name,
					status:  "connected",
					vmCount: len(vms),
					vms:     vms,
				}
			}(providerConfig.name, providerConfig.config)
		}

		// Collect results with timeout
		connectionResults := make(map[string]ProviderConnectionResult)
		collected := 0
		timeout := time.After(30 * time.Second)

		for collected < len(supportedConfigs) {
			select {
			case result := <-resultChan:
				connectionResults[result.name] = result
				if result.err == nil && len(result.vms) > 0 {
					vmCache[result.name] = result.vms
				}
				collected++

			case <-timeout:
				// Mark remaining providers as timeout/error
				for _, providerConfig := range supportedConfigs {
					if _, exists := connectionResults[providerConfig.name]; !exists {
						connectionResults[providerConfig.name] = ProviderConnectionResult{
							name:   providerConfig.name,
							status: "error",
							err:    fmt.Errorf("connection timeout"),
						}
					}
				}
				goto done
			}
		}

	done:
		return ProviderConnectionsCompletedMsg{
			connectionResults: connectionResults,
			vmCache:           vmCache,
			err:               nil,
		}
	}
}

// Render providers screen with three-panel layout
func (m AppModel) renderProviders() string {
	// Always show providers if we have them, even during loading
	if len(m.providers.providers) == 0 {
		// Only show loading spinner if we have no providers at all
		if m.providers.loading {
			var content strings.Builder

			loadingText := "Loading providers..."
			centeredLoading := lipgloss.NewStyle().
				Width(m.width).
				Align(lipgloss.Center).
				Render(loadingText)
			content.WriteString(centeredLoading + "\n\n")

			spinnerView := lipgloss.NewStyle().
				Width(m.width).
				Align(lipgloss.Center).
				Render(m.providers.spinner.View())
			content.WriteString(spinnerView)

			return content.String()
		} else {
			return lipgloss.NewStyle().
				Width(m.width).
				Height(m.height - 6).
				Align(lipgloss.Center).
				Render("No providers found\n\nPress Esc to go back")
		}
	}

	// Calculate fixed dimensions for stable layout
	totalWidth := m.width - 4
	leftWidth := totalWidth * 30 / 100
	middleWidth := totalWidth * 35 / 100
	rightWidth := totalWidth - leftWidth - middleWidth

	// Fixed height for all panels to prevent jumping
	panelHeight := m.height - 10 // Reserve space for title and instructions
	if panelHeight < 15 {
		panelHeight = 15 // Minimum height
	}

	// Update maxVisibleVMs based on actual panel height
	m.providers.maxVisibleVMs = panelHeight - 8 // Account for title, padding, borders, search

	// Build left panel (Providers) with fixed content area
	var leftContent strings.Builder
	leftTitle := "Providers"
	if m.providers.focusedPane == 0 {
		leftTitle = "🎯 " + leftTitle
	}

	// Show loading indicator in title if still loading
	if m.providers.loading {
		leftTitle += " (connecting...)"
	}

	leftContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(leftTitle) + "\n\n")

	// Calculate available lines for content (subtract title + padding)
	availableLines := panelHeight - 6 // Title, padding, borders

	// Render providers (with potential scrolling/truncation)
	for i, provider := range m.providers.providers {
		if i >= availableLines-2 { // Reserve space for potential "..." indicator
			leftContent.WriteString("... (" + fmt.Sprintf("%d more", len(m.providers.providers)-i) + ")\n")
			break
		}

		var statusIcon string
		switch provider.status {
		case "connected":
			statusIcon = "🟢"
		case "error":
			statusIcon = "🔴"
		default:
			statusIcon = "🟡"
		}

		vmCountText := ""
		if provider.vmCount > 0 {
			vmCountText = fmt.Sprintf(" (%d VMs)", provider.vmCount)
		}

		item := fmt.Sprintf("%s %s (%s)%s", statusIcon, provider.name, provider.type_, vmCountText)
		if i == m.providers.selectedProvider {
			item = selectedItemStyle.Render(item)
		} else {
			item = menuItemStyle.Render(item)
		}
		leftContent.WriteString(item + "\n")

		// If this provider is expanded, show tree-like details with navigation
		if provider.name == m.providers.expandedProvider {
			if config, exists := m.providers.providerConfigs[provider.name]; exists {
				// Tree items: 0=provider, 1=type, 2=url, 3=username, 4=password, 5=insecure
				m.providers.treeItemCount = 6

				// Tree item 1: Type
				typeItem := "    ├── Type: " + config.Type
				if m.providers.selectedTreeItem == 1 {
					leftContent.WriteString(selectedItemStyle.Render(typeItem) + "\n")
				} else {
					leftContent.WriteString(menuItemStyle.Render(typeItem) + "\n")
				}

				// Tree item 2: URL (truncated to fit on one line)
				truncatedURL := config.URL
				maxURLLength := leftWidth - 20 // Account for tree prefix and padding
				if maxURLLength > 3 && len(truncatedURL) > maxURLLength {
					truncatedURL = truncatedURL[:maxURLLength-3] + "..."
				}
				urlItem := "    ├── URL: " + truncatedURL
				if m.providers.selectedTreeItem == 2 {
					leftContent.WriteString(selectedItemStyle.Render(urlItem) + "\n")
				} else {
					leftContent.WriteString(menuItemStyle.Render(urlItem) + "\n")
				}

				// Tree item 3: Username
				usernameItem := "    ├── Username: " + config.Username
				if m.providers.selectedTreeItem == 3 {
					leftContent.WriteString(selectedItemStyle.Render(usernameItem) + "\n")
				} else {
					leftContent.WriteString(menuItemStyle.Render(usernameItem) + "\n")
				}

				// Tree item 4: Password (NO MASKING! - truncated to fit on one line)
				truncatedPassword := config.Password
				maxPasswordLength := leftWidth - 25 // Account for tree prefix and padding
				if maxPasswordLength > 3 && len(truncatedPassword) > maxPasswordLength {
					truncatedPassword = truncatedPassword[:maxPasswordLength-3] + "..."
				}
				passwordItem := "    ├── Password: " + truncatedPassword
				if m.providers.selectedTreeItem == 4 {
					leftContent.WriteString(selectedItemStyle.Render(passwordItem) + "\n")
				} else {
					leftContent.WriteString(menuItemStyle.Render(passwordItem) + "\n")
				}

				// Tree item 5: Insecure
				insecureItem := "    └── Insecure: " + fmt.Sprintf("%t", config.Insecure)
				if m.providers.selectedTreeItem == 5 {
					leftContent.WriteString(selectedItemStyle.Render(insecureItem) + "\n")
				} else {
					leftContent.WriteString(menuItemStyle.Render(insecureItem) + "\n")
				}
			}
		}
	}

	// Fill remaining space to maintain consistent height
	currentLines := strings.Count(leftContent.String(), "\n")
	for currentLines < panelHeight-4 {
		leftContent.WriteString("\n")
		currentLines++
	}

	// Build middle panel (VMs) with scrolling and search support
	var middleContent strings.Builder
	middleTitle := "Virtual Machines"
	if m.providers.focusedPane == 1 {
		middleTitle = "🎯 " + middleTitle
	}
	middleContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(middleTitle) + "\n")

	// Show search input if searching
	if m.providers.searchingVMs {
		middleContent.WriteString("\nSearch: " + m.providers.vmSearchInput.View() + "\n")
	} else {
		middleContent.WriteString("\n")
	}

	if len(m.providers.vms) == 0 {
		middleContent.WriteString("No VMs loaded\nPress Enter on provider")
	} else {
		// Determine which VMs to show based on search and scrolling
		visibleVMs := m.providers.vms
		if m.providers.searchingVMs {
			query := m.providers.vmSearchInput.Value()
			if query != "" {
				var filteredVMs []VMItem
				for _, vm := range m.providers.vms {
					if strings.Contains(strings.ToLower(vm.name), strings.ToLower(query)) ||
						strings.Contains(strings.ToLower(vm.guestOS), strings.ToLower(query)) {
						filteredVMs = append(filteredVMs, vm)
					}
				}
				visibleVMs = filteredVMs
			}
		}

		// Apply scrolling
		startIdx := m.providers.vmScrollOffset
		endIdx := startIdx + m.providers.maxVisibleVMs
		if endIdx > len(visibleVMs) {
			endIdx = len(visibleVMs)
		}
		if startIdx > len(visibleVMs) {
			startIdx = 0
			endIdx = 0
		}

		// Show scroll indicators
		if startIdx > 0 {
			middleContent.WriteString("... (" + fmt.Sprintf("%d above", startIdx) + ")\n")
		}

		// Render visible VMs
		for i := startIdx; i < endIdx; i++ {
			vm := visibleVMs[i]
			var statusIcon string
			if vm.isRunning {
				statusIcon = "▶️"
			} else {
				statusIcon = "⏸️"
			}

			item := fmt.Sprintf("%s %s", statusIcon, vm.name)
			// Highlight selection based on actual index in original list
			if !m.providers.searchingVMs && i == m.providers.selectedVM {
				item = selectedItemStyle.Render(item)
			} else {
				item = menuItemStyle.Render(item)
			}
			middleContent.WriteString(item + "\n")
		}

		// Show bottom scroll indicator
		if endIdx < len(visibleVMs) {
			middleContent.WriteString("... (" + fmt.Sprintf("%d below", len(visibleVMs)-endIdx) + ")\n")
		}
	}

	// Fill remaining space to maintain consistent height
	currentLines = strings.Count(middleContent.String(), "\n")
	for currentLines < panelHeight-4 {
		middleContent.WriteString("\n")
		currentLines++
	}

	// Build right panel (Details) with VM details OR provider credentials
	var rightContent strings.Builder
	rightTitle := "Details"
	if m.providers.focusedPane == 2 {
		rightTitle = "🎯 " + rightTitle
	}
	rightContent.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Align(lipgloss.Center).
		Render(rightTitle) + "\n\n")

	if m.providers.detailView.vm != nil {
		// Show VM details
		vm := m.providers.detailView.vm
		rightContent.WriteString(Field("Name", vm.Name) + "\n")
		rightContent.WriteString(Field("Provider", vm.Provider) + "\n")
		rightContent.WriteString(Field("Status", vm.PowerState) + "\n")
		rightContent.WriteString(Field("CPU", fmt.Sprintf("%d cores", vm.CPU)) + "\n")
		rightContent.WriteString(Field("Memory", fmt.Sprintf("%d MB", vm.MemoryMB)) + "\n")
		rightContent.WriteString(Field("Storage", fmt.Sprintf("%.1f GB", vm.StorageGB)) + "\n")
		rightContent.WriteString(Field("OS", vm.GuestOS) + "\n")
		if len(vm.IPAddresses) > 0 {
			rightContent.WriteString(Field("IP", vm.IPAddresses[0]) + "\n")
		}
		rightContent.WriteString("\n" + lipgloss.NewStyle().Foreground(lipgloss.Color("240")).Render("Press Enter to copy VM name") + "\n")
	} else if len(m.providers.providers) > 0 {
		// Show provider credentials
		selectedProvider := m.providers.providers[m.providers.selectedProvider]
		rightContent.WriteString(WideField("Provider", selectedProvider.name) + "\n")
		rightContent.WriteString(WideField("Type", selectedProvider.type_) + "\n")
		rightContent.WriteString(WideField("Status", selectedProvider.status) + "\n")

		if config, exists := m.providers.providerConfigs[selectedProvider.name]; exists {
			rightContent.WriteString(WideField("URL", config.URL) + "\n")
			rightContent.WriteString(WideField("Username", config.Username) + "\n")
			// Mask password for security
			maskedPassword := strings.Repeat("*", len(config.Password))
			if len(maskedPassword) > 20 {
				maskedPassword = maskedPassword[:20] + "..."
			}
			rightContent.WriteString(WideField("Password", maskedPassword) + "\n")
			rightContent.WriteString(WideField("Insecure", fmt.Sprintf("%t", config.Insecure)) + "\n")
		}
	} else {
		rightContent.WriteString("No selection")
	}

	// Fill remaining space to maintain consistent height
	currentLines = strings.Count(rightContent.String(), "\n")
	for currentLines < panelHeight-4 {
		rightContent.WriteString("\n")
		currentLines++
	}

	// Combine panels with borders and FIXED HEIGHT
	leftPanel := lipgloss.NewStyle().
		Width(leftWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(leftContent.String())

	middlePanel := lipgloss.NewStyle().
		Width(middleWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(middleContent.String())

	rightPanel := lipgloss.NewStyle().
		Width(rightWidth).
		Height(panelHeight).
		Border(lipgloss.NormalBorder()).
		BorderForeground(lipgloss.Color("240")).
		Padding(1).
		Render(rightContent.String())

	// Combine panels horizontally
	mainContent := lipgloss.JoinHorizontal(lipgloss.Top, leftPanel, middlePanel, rightPanel)

	// Add title above panels
	var content strings.Builder
	title := "=== VM Providers and Virtual Machines ==="
	centeredTitle := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render(title)
	content.WriteString(centeredTitle + "\n\n")

	content.WriteString(mainContent)

	// Add navigation instructions
	content.WriteString("\n\n")
	var instructions string
	if m.providers.searchingVMs {
		instructions = "Search: Type to filter • Enter Exit search • Esc Cancel search"
	} else {
		// Check if we have an expanded provider with tree navigation
		hasExpandedTree := m.providers.expandedProvider != "" && m.providers.selectedTreeItem > 0
		if hasExpandedTree {
			instructions = "Tree Navigation: ↑/↓ Navigate Tree • Enter Copy Field • ← Collapse • Tab/Shift+Tab Switch Panel • / Search VMs • R Refresh • Esc Back"
		} else {
			instructions = "Navigation: ↑/↓ Navigate • → Expand Tree • Tab/Shift+Tab Switch Panel • / Search VMs • R Refresh • Esc Back"
		}
	}

	centeredInstructions := lipgloss.NewStyle().
		Width(m.width).
		Align(lipgloss.Center).
		Foreground(lipgloss.Color("240")).
		Render(instructions)
	content.WriteString(centeredInstructions)

	return content.String()
}

// Command to load VMs for a specific provider
func (m AppModel) loadVMsForProviderCmd(providerName string) tea.Cmd {
	return func() tea.Msg {
		// Load provider configurations
		providerConfigs, err := providerLoaderDeps.LoadProviderConfigs()
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to load provider configurations: %w", err),
			}
		}

		// Find the specific provider
		providerConfig, exists := providerConfigs[providerName]
		if !exists {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("provider '%s' not found", providerName),
			}
		}

		// Create the provider
		provider, err := providerLoaderDeps.CreateProvider(
			providerConfig.Type,
			providerConfig.URL,
			providerConfig.Username,
			providerConfig.Password,
			providerConfig.Insecure,
			providerConfig.ExtraParams,
		)
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to create provider: %w", err),
			}
		}

		// Connect to the provider
		if err := provider.Connect(); err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to connect to provider: %w", err),
			}
		}
		defer func() { _ = provider.Close() }()

		// List VMs
		ctx := context.Background()
		vms, err := provider.ListVMs(ctx)
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to list VMs: %w", err),
			}
		}

		return VMsLoadedMsg{
			providerName: providerName,
			vms:          vms,
			err:          nil,
		}
	}
}

// Command to load VMs for a provider with caching support
func (m AppModel) loadVMsForProviderWithCacheCmd(providerName string) tea.Cmd {
	return func() tea.Msg {
		// Check cache first
		if cachedVMs, exists := m.providers.vmCache[providerName]; exists {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          cachedVMs,
				err:          nil,
			}
		}

		// Load provider configurations
		providerConfigs, err := providerLoaderDeps.LoadProviderConfigs()
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to load provider configurations: %w", err),
			}
		}

		// Find the specific provider
		providerConfig, exists := providerConfigs[providerName]
		if !exists {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("provider '%s' not found", providerName),
			}
		}

		// Create the provider
		provider, err := providerLoaderDeps.CreateProvider(
			providerConfig.Type,
			providerConfig.URL,
			providerConfig.Username,
			providerConfig.Password,
			providerConfig.Insecure,
			providerConfig.ExtraParams,
		)
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to create provider: %w", err),
			}
		}

		// Connect to the provider
		if err := provider.Connect(); err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to connect to provider: %w", err),
			}
		}
		defer func() { _ = provider.Close() }()

		// List VMs
		ctx := context.Background()
		vms, err := provider.ListVMs(ctx)
		if err != nil {
			return VMsLoadedMsg{
				providerName: providerName,
				vms:          []VMInfo{},
				err:          fmt.Errorf("failed to list VMs: %w", err),
			}
		}

		return VMsLoadedMsg{
			providerName: providerName,
			vms:          vms,
			err:          nil,
		}
	}
}

// Render provider detail screen with tree-like expandable interface
func (m AppModel) renderProviderDetail() string {
	if m.providerDetail.loading {
		loadingContent := StatusLoading("Loading provider details...") + "\n\n" + m.providerDetail.spinner.View()
		return CenteredContainer(loadingContent, 40)
	}

	if m.providerDetail.config == nil {
		return "No provider configuration available."
	}

	var content strings.Builder
	config := m.providerDetail.config

	// Title
	content.WriteString(Header(fmt.Sprintf("Provider Details -- [%s]", m.providerDetail.providerName)) + "\n\n")

	// Provider type badge
	content.WriteString(lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("32")).
		Render("🔧 "+strings.ToUpper(config.Type)+" Provider") + "\n\n")

	// Tree-like interface - create expandable sections
	theme := GetCurrentTheme()
	selectedCursor := m.providerDetail.table.Cursor()

	// Define all fields with their display names and values
	fields := []struct {
		name  string
		value string
	}{
		{"Provider Name", m.providerDetail.providerName},
		{"Type", config.Type},
		{"URL", config.URL},
		{"Username", config.Username},
		{"Password", config.Password},
		{"Insecure", fmt.Sprintf("%t", config.Insecure)},
	}

	// Add extra parameters if they exist
	for key, value := range config.ExtraParams {
		fields = append(fields, struct {
			name  string
			value string
		}{key, value})
	}

	// Render each field as a tree item
	for i, field := range fields {
		var line string

		if i == selectedCursor {
			// Selected item - show with arrow and highlighted
			line = lipgloss.NewStyle().
				Foreground(theme.SelectionFg).
				Background(theme.Selection).
				Render(fmt.Sprintf("→ %-15s %s", field.name+":", field.value))
		} else {
			// Normal item - show with bullet
			line = lipgloss.NewStyle().
				Foreground(theme.Primary).
				Render(fmt.Sprintf("  %-15s %s", field.name+":", field.value))
		}

		content.WriteString(line + "\n")
	}

	// Add copy instruction
	content.WriteString("\n💡 Use ↑↓ to navigate • Enter to copy value to clipboard • Esc to go back")

	return content.String()
}
