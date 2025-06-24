# Makefile for mtv-dev CLI

BINARY=mtv-dev
COVERAGE=coverage.out
COVERAGE_HTML=coverage.html

.PHONY: all build install test test-verbose test-coverage test-tui test-main clean help

# Default target
all: build

# Build the binary
build:
	@echo "Building $(BINARY)..."
	go build -o $(BINARY) .

# Alias for build (legacy compatibility)
install: build

# Run all tests with coverage
test:
	@echo "Running all tests with coverage..."
	go test -coverprofile=$(COVERAGE) ./...
	@echo "Coverage summary:"
	@go tool cover -func=$(COVERAGE) | grep total: | awk '{print "Total coverage: " $$3}'

# Run tests with verbose output (useful for debugging)
test-verbose:
	@echo "Running all tests with verbose output..."
	go test -v ./...

# Generate and view HTML coverage report
test-coverage:
	@echo "Generating coverage report..."
	go test -coverprofile=$(COVERAGE) ./...
	go tool cover -html=$(COVERAGE) -o $(COVERAGE_HTML)
	@echo "Coverage report generated: $(COVERAGE_HTML)"

# Run only TUI tests
test-tui:
	@echo "Running TUI tests..."
	go test -v ./tui/

# Run only main package tests
test-main:
	@echo "Running main package tests..."
	go test -v .

# Run tests with gotestsum (pretty output) - requires gotestsum
test-pretty:
	@command -v gotestsum >/dev/null 2>&1 || { echo "Installing gotestsum..."; go install gotest.tools/gotestsum@latest; }
	@echo "Running tests with pretty output..."
	gotestsum -- -coverprofile=$(COVERAGE) ./...
	@echo "Coverage summary:"
	@go tool cover -func=$(COVERAGE) | grep total: | awk '{print "Total coverage: " $$3}'

# Clean build artifacts and coverage files
clean:
	@echo "Cleaning build artifacts..."
	rm -f $(BINARY) $(COVERAGE) $(COVERAGE_HTML)

# Display help
help:
	@echo "Available targets:"
	@echo "  build         - Build the $(BINARY) binary"
	@echo "  install       - Alias for build (legacy compatibility)"
	@echo "  test          - Run all tests with coverage summary"
	@echo "  test-verbose  - Run all tests with verbose output"
	@echo "  test-coverage - Generate HTML coverage report"
	@echo "  test-tui      - Run only TUI tests"
	@echo "  test-main     - Run only main package tests"
	@echo "  test-pretty   - Run tests with pretty output (requires gotestsum)"
	@echo "  clean         - Remove build artifacts and coverage files"
	@echo "  help          - Show this help message"
	@echo ""
	@echo "Test structure:"
	@echo "  main_test.go      - Main package tests (CLI commands, validation)"
	@echo "  tui/models_test.go - TUI tests (interface, interaction, state)"
