package tui

import (
	"github.com/charmbracelet/lipgloss"
)

// Color definitions - Minimalistic dark theme
var (
	primaryColor = lipgloss.Color("#E0E0E0") // Light gray
	accentColor  = lipgloss.Color("#6C7B7F") // Muted blue-gray
	successColor = lipgloss.Color("#8F9F8F") // Muted green
	errorColor   = lipgloss.Color("#B57C7C") // Muted red
	warningColor = lipgloss.Color("#B5A68B") // Muted yellow
	mutedColor   = lipgloss.Color("#6B6B6B") // Dark gray
	subtleColor  = lipgloss.Color("#4A4A4A") // Very dark gray
	bgColor      = lipgloss.Color("#1C1C1C") // Dark background
)

// Base styles
var (
	titleStyle = lipgloss.NewStyle().
			Foreground(primaryColor).
			Bold(true).
			Align(lipgloss.Center).
			Padding(0, 1)

	headerStyle = lipgloss.NewStyle().
			Foreground(accentColor).
			Bold(true)

	menuItemStyle = lipgloss.NewStyle().
			Foreground(primaryColor).
			Padding(0, 2).
			Margin(0)

	selectedItemStyle = lipgloss.NewStyle().
				Foreground(bgColor).
				Background(accentColor).
				Padding(0, 2).
				Margin(0)

	helpStyle = lipgloss.NewStyle().
			Foreground(mutedColor).
			Align(lipgloss.Center).
			Padding(1, 0)

	errorStyle = lipgloss.NewStyle().
			Foreground(errorColor)

	successStyle = lipgloss.NewStyle().
			Foreground(successColor)

	warningStyle = lipgloss.NewStyle().
			Foreground(warningColor)
)

// Full-screen layout styles
var (
	fullScreenStyle = lipgloss.NewStyle()

	centeredContainerStyle = lipgloss.NewStyle().
				Align(lipgloss.Center).
				AlignVertical(lipgloss.Center)

	mainContainerStyle = lipgloss.NewStyle().
				Padding(2, 4).
				Align(lipgloss.Center)

	headerContainerFullStyle = lipgloss.NewStyle().
					Foreground(primaryColor).
					Bold(true).
					Align(lipgloss.Center).
					Padding(1, 0).
					Margin(0, 0, 2, 0)

	footerContainerStyle = lipgloss.NewStyle().
				Foreground(mutedColor).
				Align(lipgloss.Center).
				Padding(1, 0).
				Margin(2, 0, 0, 0)
)

// Status indicator styles
var (
	statusOnlineStyle = lipgloss.NewStyle().
				Foreground(successColor)

	statusOfflineStyle = lipgloss.NewStyle().
				Foreground(errorColor)

	statusWarningStyle = lipgloss.NewStyle().
				Foreground(warningColor)
)

// Container styles
var (
	containerStyle = lipgloss.NewStyle().
			Padding(1, 2).
			Margin(0, 0, 1, 0)

	headerContainerStyle = lipgloss.NewStyle().
				Foreground(primaryColor).
				Bold(true).
				Align(lipgloss.Center).
				Padding(1, 0).
				Margin(0, 0, 1, 0)
)

// Progress and loading styles
var (
	spinnerStyle = lipgloss.NewStyle().
		Foreground(accentColor)
)

// Card and section styles for TUI
var (
	sectionHeaderStyle = lipgloss.NewStyle().
				Foreground(accentColor).
				Bold(true).
				Margin(0, 0, 0, 0)

	fieldLabelStyle = lipgloss.NewStyle().
			Foreground(mutedColor).
			Bold(true).
			Width(12).
			Align(lipgloss.Right)

	fieldValueStyle = lipgloss.NewStyle().
			Foreground(primaryColor)

	codeBlockStyle = lipgloss.NewStyle().
			Background(subtleColor).
			Foreground(primaryColor).
			Padding(1, 2).
			Margin(0, 0, 1, 0).
			Border(lipgloss.NormalBorder()).
			BorderForeground(mutedColor)

	infoCardStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(successColor).
			Padding(1, 2).
			Margin(0, 0, 1, 0)

	accessCardStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(warningColor).
			Padding(1, 2).
			Margin(0, 0, 1, 0)

	commandCardStyle = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(accentColor).
				Padding(1, 2).
				Margin(0, 0, 1, 0)
)

// Helper functions for styled text
func StatusOnline(text string) string {
	return statusOnlineStyle.Render("● " + text)
}

func StatusOffline(text string) string {
	return statusOfflineStyle.Render("● " + text)
}

func StatusWarning(text string) string {
	return statusWarningStyle.Render("● " + text)
}

func StatusLoading(text string) string {
	return spinnerStyle.Render("◦ " + text)
}

func Title(text string) string {
	return titleStyle.Render(text)
}

func Header(text string) string {
	return headerStyle.Render(text)
}

func Error(text string) string {
	return errorStyle.Render("● " + text)
}

func Success(text string) string {
	return successStyle.Render("● " + text)
}

func Warning(text string) string {
	return warningStyle.Render("● " + text)
}

func Help(text string) string {
	return helpStyle.Render(text)
}

func Container(content string) string {
	return containerStyle.Render(content)
}

func HeaderContainer(content string) string {
	return headerContainerStyle.Render(content)
}

// Full-screen layout helpers
func FullScreenContainer(content string, width, height int) string {
	return fullScreenStyle.
		Width(width).
		Height(height).
		Render(content)
}

func CenteredContainer(content string, width int) string {
	return centeredContainerStyle.
		Width(width).
		Render(content)
}

func MainContainer(content string, width int) string {
	// Calculate a reasonable max width for content (80% of screen, but not more than 120 chars)
	maxWidth := width * 8 / 10
	if maxWidth > 120 {
		maxWidth = 120
	}
	return mainContainerStyle.
		Width(maxWidth).
		Render(content)
}

func HeaderContainerFull(content string, width int) string {
	return headerContainerFullStyle.
		Width(width).
		Render(content)
}

func FooterContainerFull(content string, width int) string {
	return footerContainerStyle.
		Width(width).
		Render(content)
}

// TUI Card helper functions with responsive width
func InfoCard(title, content string) string {
	return infoCardStyle.Render(sectionHeaderStyle.Render("🔍 "+title) + "\n\n" + content)
}

func AccessCard(title, content string) string {
	return accessCardStyle.Render(sectionHeaderStyle.Render("🔐 "+title) + "\n\n" + content)
}

func CommandCard(title, content string) string {
	return commandCardStyle.Render(sectionHeaderStyle.Render("⚡ "+title) + "\n\n" + content)
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
		return fieldLabelStyle.Render(label+":") + " " + fieldValueStyle.Render(wrappedValue)
	}
	return fieldLabelStyle.Render(label+":") + " " + fieldValueStyle.Render(value)
}

func Field(label, value string) string {
	return fieldLabelStyle.Render(label+":") + " " + fieldValueStyle.Render(value)
}

func CodeBlock(content string) string {
	return codeBlockStyle.Render(content)
}

func SectionHeader(text string) string {
	return sectionHeaderStyle.Render(text)
}
