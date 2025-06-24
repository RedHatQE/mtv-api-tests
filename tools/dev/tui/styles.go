package tui

import (
	"github.com/charmbracelet/lipgloss"
)

// Helper functions to get themed colors
func getTheme() Theme {
	return GetCurrentTheme()
}

// Base styles with theme support
func getTitleStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Primary).
		Bold(true).
		Align(lipgloss.Center).
		Padding(0, 1)
}

func getHeaderStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Header).
		Bold(true)
}

func getMenuItemStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Primary).
		Padding(0, 2).
		Margin(0)
}

func getSelectedItemStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.SelectionFg).
		Background(theme.Selection).
		Padding(0, 2).
		Margin(0)
}

func getHelpStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Muted).
		Align(lipgloss.Center).
		Padding(1, 0)
}

func getErrorStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Error)
}

func getSuccessStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Success)
}

func getWarningStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Warning)
}

// Legacy variables for compatibility (will be updated to use functions)
var (
	// Keep only the ones that are still used
	titleStyle        = getTitleStyle()
	menuItemStyle     = getMenuItemStyle()
	selectedItemStyle = getSelectedItemStyle()
)

// Update styles when theme changes
func UpdateStyles() {
	titleStyle = getTitleStyle()
	menuItemStyle = getMenuItemStyle()
	selectedItemStyle = getSelectedItemStyle()
}

// Full-screen layout styles
func getFullScreenStyle() lipgloss.Style {
	return lipgloss.NewStyle()
}

func getCenteredContainerStyle() lipgloss.Style {
	return lipgloss.NewStyle().
		Align(lipgloss.Center).
		AlignVertical(lipgloss.Center)
}

func getMainContainerStyle() lipgloss.Style {
	return lipgloss.NewStyle().
		Padding(2, 4).
		Align(lipgloss.Center)
}

func getHeaderContainerFullStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Primary).
		Bold(true).
		Align(lipgloss.Center).
		Padding(1, 0).
		Margin(0, 0, 2, 0)
}

func getFooterContainerStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Muted).
		Align(lipgloss.Center).
		Padding(1, 0).
		Margin(2, 0, 0, 0)
}

// Status indicator styles
func getStatusOnlineStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.StatusOnline)
}

func getStatusOfflineStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.StatusOffline)
}

func getStatusWarningStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.StatusWarning)
}

// Container styles
func getContainerStyle() lipgloss.Style {
	return lipgloss.NewStyle().
		Padding(1, 2).
		Margin(0, 0, 1, 0)
}

func getHeaderContainerStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Primary).
		Bold(true).
		Align(lipgloss.Center).
		Padding(1, 0).
		Margin(0, 0, 1, 0)
}

// Progress and loading styles
func getSpinnerStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Accent)
}

// Card and section styles for TUI
func getSectionHeaderStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Header).
		Bold(true).
		Margin(0, 0, 0, 0)
}

func getFieldLabelStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Muted).
		Bold(true).
		Width(12).
		Align(lipgloss.Right)
}

func getFieldValueStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Foreground(theme.Primary)
}

func getCodeBlockStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Background(theme.Subtle).
		Foreground(theme.Primary).
		Padding(1, 2).
		Margin(0, 0, 1, 0).
		Border(lipgloss.NormalBorder()).
		BorderForeground(theme.Border)
}

func getInfoCardStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(theme.Success).
		Padding(1, 2).
		Margin(0, 0, 1, 0)
}

func getAccessCardStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(theme.Warning).
		Padding(1, 2).
		Margin(0, 0, 1, 0)
}

func getCommandCardStyle() lipgloss.Style {
	theme := getTheme()
	return lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(theme.Accent).
		Padding(1, 2).
		Margin(0, 0, 1, 0)
}

// Helper functions for styled text
func StatusOnline(text string) string {
	return getStatusOnlineStyle().Render("● " + text)
}

func StatusOffline(text string) string {
	return getStatusOfflineStyle().Render("● " + text)
}

func StatusWarning(text string) string {
	return getStatusWarningStyle().Render("● " + text)
}

func StatusLoading(text string) string {
	return getSpinnerStyle().Render("◦ " + text)
}

func Title(text string) string {
	return getTitleStyle().Render(text)
}

func Header(text string) string {
	return getHeaderStyle().Render(text)
}

func Error(text string) string {
	return getErrorStyle().Render("● " + text)
}

func Success(text string) string {
	return getSuccessStyle().Render("● " + text)
}

func Warning(text string) string {
	return getWarningStyle().Render("● " + text)
}

func Help(text string) string {
	return getHelpStyle().Render(text)
}

func Container(content string) string {
	return getContainerStyle().Render(content)
}

func HeaderContainer(content string) string {
	return getHeaderContainerStyle().Render(content)
}

// Full-screen layout helpers
func FullScreenContainer(content string, width, height int) string {
	return getFullScreenStyle().
		Width(width).
		Height(height).
		Render(content)
}

func CenteredContainer(content string, width int) string {
	return getCenteredContainerStyle().
		Width(width).
		Render(content)
}

func MainContainer(content string, width int) string {
	// Calculate a reasonable max width for content (80% of screen, but not more than 120 chars)
	maxWidth := width * 8 / 10
	if maxWidth > 120 {
		maxWidth = 120
	}
	return getMainContainerStyle().
		Width(maxWidth).
		Render(content)
}

func HeaderContainerFull(content string, width int) string {
	return getHeaderContainerFullStyle().
		Width(width).
		Render(content)
}

func FooterContainerFull(content string, width int) string {
	return getFooterContainerStyle().
		Width(width).
		Render(content)
}

// TUI Card helper functions with responsive width
func InfoCard(title, content string) string {
	return getInfoCardStyle().Render(getSectionHeaderStyle().Render("🔍 "+title) + "\n\n" + content)
}

func AccessCard(title, content string) string {
	return getAccessCardStyle().Render(getSectionHeaderStyle().Render("🔐 "+title) + "\n\n" + content)
}

func CommandCard(title, content string) string {
	return getCommandCardStyle().Render(getSectionHeaderStyle().Render("⚡ "+title) + "\n\n" + content)
}

// Responsive field that handles long values
func ResponsiveField(label, value string, maxWidth int) string {
	// If value is too long, wrap it nicely
	if len(value) > maxWidth-15 {
		wrappedValue := value
		if len(value) > maxWidth-15 {
			// For very long values like URLs or IIBs, break at reasonable points
			if len(value) > 60 {
				wrappedValue = value[:57] + "..."
			}
		}
		return getFieldLabelStyle().Render(label+":") + " " + getFieldValueStyle().Render(wrappedValue)
	}
	return getFieldLabelStyle().Render(label+":") + " " + getFieldValueStyle().Render(value)
}

func Field(label, value string) string {
	return getFieldLabelStyle().Render(label+":") + " " + getFieldValueStyle().Render(value)
}

func CodeBlock(content string) string {
	return getCodeBlockStyle().Render(content)
}

func SectionHeader(text string) string {
	return getSectionHeaderStyle().Render(text)
}
