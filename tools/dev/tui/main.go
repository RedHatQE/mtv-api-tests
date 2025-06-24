package tui

import (
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"
)

// StartTUI initializes and runs the TUI application
func StartTUI() error {
	// Create the model
	model := NewAppModel()

	// Create the tea program
	p := tea.NewProgram(
		model,
		tea.WithAltScreen(),       // Use alternate screen buffer
		tea.WithMouseCellMotion(), // Enable mouse support
	)

	// Run the program
	_, err := p.Run()
	if err != nil {
		return fmt.Errorf("TUI error: %w", err)
	}

	return nil
}

// RunTUI is a convenience function that starts the TUI and handles errors
func RunTUI() {
	if err := StartTUI(); err != nil {
		fmt.Fprintf(os.Stderr, "Error running TUI: %v\n", err)
		os.Exit(1)
	}
}
