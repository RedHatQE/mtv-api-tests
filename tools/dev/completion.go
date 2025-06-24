package main

import (
	"io/fs"
	"os"
	"sort"
	"strings"

	"github.com/spf13/cobra"
)

// getClusterNames provides tab completion for cluster names
func getClusterNames(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
	files, err := readDir(CLUSTERS_PATH)
	if err != nil {
		return nil, cobra.ShellCompDirectiveError
	}
	var names []string
	for _, f := range files {
		name := f.Name()
		if f.IsDir() && strings.HasPrefix(name, toComplete) && (strings.HasPrefix(name, "qemtv-") || strings.HasPrefix(name, "qemtvd-")) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names, cobra.ShellCompDirectiveNoFileComp
}

// getProviderNames provides tab completion for provider names
func getProviderNames(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
	var providers []string
	for provider := range providerMap {
		if strings.HasPrefix(provider, toComplete) {
			providers = append(providers, provider)
		}
	}
	sort.Strings(providers)
	return providers, cobra.ShellCompDirectiveNoFileComp
}

// getStorageNames provides tab completion for storage names
func getStorageNames(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
	var storageTypes []string
	for storage := range storageMap {
		if strings.HasPrefix(storage, toComplete) {
			storageTypes = append(storageTypes, storage)
		}
	}
	sort.Strings(storageTypes)
	return storageTypes, cobra.ShellCompDirectiveNoFileComp
}

// getTemplateNames provides tab completion for template names
func getTemplateNames(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
	var templates []string
	for template := range runsTemplates {
		if strings.HasPrefix(template, toComplete) {
			templates = append(templates, template)
		}
	}
	sort.Strings(templates)
	return templates, cobra.ShellCompDirectiveNoFileComp
}

// readDir is a variable for testability - allows mocking in tests
var readDir = func(path string) ([]fs.DirEntry, error) {
	return os.ReadDir(path)
}
