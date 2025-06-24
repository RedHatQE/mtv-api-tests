package tui

import (
	"github.com/charmbracelet/lipgloss"
)

// Theme defines all colors used in the TUI
type Theme struct {
	Name string

	// Base colors
	Primary    lipgloss.Color // Light text
	Secondary  lipgloss.Color // Muted text
	Accent     lipgloss.Color // Highlight/focus color
	Success    lipgloss.Color // Success indicators
	Warning    lipgloss.Color // Warning indicators
	Error      lipgloss.Color // Error indicators
	Muted      lipgloss.Color // Very muted text
	Subtle     lipgloss.Color // Borders and subtle elements
	Background lipgloss.Color // Dark background

	// Semantic colors
	Border        lipgloss.Color // Border colors
	Selection     lipgloss.Color // Selection background
	SelectionFg   lipgloss.Color // Selection foreground
	Header        lipgloss.Color // Header text
	StatusOnline  lipgloss.Color // Online status
	StatusOffline lipgloss.Color // Offline status
	StatusWarning lipgloss.Color // Warning status
}

// Available themes
var (
	// Dark theme (current elegant theme)
	DarkTheme = Theme{
		Name:          "Dark",
		Primary:       lipgloss.Color("#E0E0E0"), // Light gray
		Secondary:     lipgloss.Color("#B0B0B0"), // Medium gray
		Accent:        lipgloss.Color("#6C7B7F"), // Muted blue-gray
		Success:       lipgloss.Color("#8F9F8F"), // Muted green
		Warning:       lipgloss.Color("#B5A68B"), // Muted yellow
		Error:         lipgloss.Color("#B57C7C"), // Muted red
		Muted:         lipgloss.Color("#6B6B6B"), // Dark gray
		Subtle:        lipgloss.Color("#4A4A4A"), // Very dark gray
		Background:    lipgloss.Color("#1C1C1C"), // Dark background
		Border:        lipgloss.Color("#6B6B6B"), // Same as muted
		Selection:     lipgloss.Color("#6C7B7F"), // Same as accent
		SelectionFg:   lipgloss.Color("#1C1C1C"), // Same as background
		Header:        lipgloss.Color("#6C7B7F"), // Same as accent
		StatusOnline:  lipgloss.Color("#8F9F8F"), // Same as success
		StatusOffline: lipgloss.Color("#B57C7C"), // Same as error
		StatusWarning: lipgloss.Color("#B5A68B"), // Same as warning
	}

	// Light theme
	LightTheme = Theme{
		Name:          "Light",
		Primary:       lipgloss.Color("#1A1A1A"), // Very dark gray (almost black) for main text
		Secondary:     lipgloss.Color("#4A4A4A"), // Dark gray for secondary text
		Accent:        lipgloss.Color("#0066CC"), // Darker blue for better contrast
		Success:       lipgloss.Color("#28A745"), // Darker green
		Warning:       lipgloss.Color("#FFC107"), // Bright yellow/orange
		Error:         lipgloss.Color("#DC3545"), // Bright red
		Muted:         lipgloss.Color("#6C757D"), // Medium gray for muted text
		Subtle:        lipgloss.Color("#E9ECEF"), // Very light gray for subtle elements
		Background:    lipgloss.Color("#FFFFFF"), // Pure white background
		Border:        lipgloss.Color("#DEE2E6"), // Light gray borders
		Selection:     lipgloss.Color("#0066CC"), // Blue selection background
		SelectionFg:   lipgloss.Color("#FFFFFF"), // White text on blue selection
		Header:        lipgloss.Color("#0066CC"), // Blue headers
		StatusOnline:  lipgloss.Color("#28A745"), // Green for online
		StatusOffline: lipgloss.Color("#DC3545"), // Red for offline
		StatusWarning: lipgloss.Color("#FFC107"), // Yellow for warnings
	}

	// Blue theme
	BlueTheme = Theme{
		Name:          "Blue",
		Primary:       lipgloss.Color("#E8F4FD"), // Very light blue
		Secondary:     lipgloss.Color("#B3D9F7"), // Light blue
		Accent:        lipgloss.Color("#1E88E5"), // Bright blue
		Success:       lipgloss.Color("#66BB6A"), // Green
		Warning:       lipgloss.Color("#FFB74D"), // Orange
		Error:         lipgloss.Color("#EF5350"), // Red
		Muted:         lipgloss.Color("#78909C"), // Blue gray
		Subtle:        lipgloss.Color("#37474F"), // Dark blue gray
		Background:    lipgloss.Color("#0D1117"), // Very dark blue
		Border:        lipgloss.Color("#30363D"), // Dark blue border
		Selection:     lipgloss.Color("#1E88E5"), // Bright blue selection
		SelectionFg:   lipgloss.Color("#FFFFFF"), // White text
		Header:        lipgloss.Color("#1E88E5"), // Bright blue
		StatusOnline:  lipgloss.Color("#66BB6A"), // Green
		StatusOffline: lipgloss.Color("#EF5350"), // Red
		StatusWarning: lipgloss.Color("#FFB74D"), // Orange
	}

	// Neon theme
	NeonTheme = Theme{
		Name:          "Neon",
		Primary:       lipgloss.Color("#00FF00"), // Bright green
		Secondary:     lipgloss.Color("#80FF80"), // Light green
		Accent:        lipgloss.Color("#FF00FF"), // Magenta
		Success:       lipgloss.Color("#00FFFF"), // Cyan
		Warning:       lipgloss.Color("#FFFF00"), // Yellow
		Error:         lipgloss.Color("#FF0080"), // Hot pink
		Muted:         lipgloss.Color("#808080"), // Gray
		Subtle:        lipgloss.Color("#404040"), // Dark gray
		Background:    lipgloss.Color("#000000"), // Black
		Border:        lipgloss.Color("#808080"), // Gray border
		Selection:     lipgloss.Color("#FF00FF"), // Magenta selection
		SelectionFg:   lipgloss.Color("#000000"), // Black text
		Header:        lipgloss.Color("#00FFFF"), // Cyan headers
		StatusOnline:  lipgloss.Color("#00FFFF"), // Cyan
		StatusOffline: lipgloss.Color("#FF0080"), // Hot pink
		StatusWarning: lipgloss.Color("#FFFF00"), // Yellow
	}

	// Classic Light theme - more traditional light theme
	ClassicLightTheme = Theme{
		Name:          "Classic Light",
		Primary:       lipgloss.Color("#000000"), // Pure black for maximum readability
		Secondary:     lipgloss.Color("#333333"), // Dark gray for secondary text
		Accent:        lipgloss.Color("#0052CC"), // Classic blue
		Success:       lipgloss.Color("#006600"), // Dark green
		Warning:       lipgloss.Color("#FF8800"), // Orange
		Error:         lipgloss.Color("#CC0000"), // Dark red
		Muted:         lipgloss.Color("#666666"), // Medium gray
		Subtle:        lipgloss.Color("#F5F5F5"), // Very light gray background
		Background:    lipgloss.Color("#FFFFFF"), // Pure white
		Border:        lipgloss.Color("#CCCCCC"), // Standard gray border
		Selection:     lipgloss.Color("#0052CC"), // Blue selection
		SelectionFg:   lipgloss.Color("#FFFFFF"), // White text on selection
		Header:        lipgloss.Color("#0052CC"), // Blue headers
		StatusOnline:  lipgloss.Color("#006600"), // Dark green
		StatusOffline: lipgloss.Color("#CC0000"), // Dark red
		StatusWarning: lipgloss.Color("#FF8800"), // Orange
	}
)

// Current active theme
var currentTheme = DarkTheme

// Get current theme
func GetCurrentTheme() Theme {
	return currentTheme
}

// Set current theme
func SetTheme(theme Theme) {
	currentTheme = theme
}

// Available theme names
func GetAvailableThemes() []string {
	return []string{
		DarkTheme.Name,
		LightTheme.Name,
		BlueTheme.Name,
		NeonTheme.Name,
		ClassicLightTheme.Name,
	}
}

// Get theme by name
func GetThemeByName(name string) *Theme {
	switch name {
	case "Dark":
		return &DarkTheme
	case "Light":
		return &LightTheme
	case "Blue":
		return &BlueTheme
	case "Neon":
		return &NeonTheme
	case "Classic Light":
		return &ClassicLightTheme
	default:
		return &DarkTheme
	}
}
