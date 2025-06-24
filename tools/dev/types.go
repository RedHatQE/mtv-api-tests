package main

import (
	"math/rand"
	"time"

	configv1 "github.com/openshift/client-go/config/clientset/versioned/typed/config/v1"
	routev1 "github.com/openshift/client-go/route/clientset/versioned/typed/route/v1"
	"github.com/spf13/cobra"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// Color constants for output formatting
const (
	ColorReset  = "\033[0m"
	ColorRed    = "\033[31m"
	ColorGreen  = "\033[32m"
	ColorYellow = "\033[33m"
	ColorBlue   = "\033[34m"
	ColorPurple = "\033[35m"
	ColorCyan   = "\033[36m"
	ColorWhite  = "\033[37m"
)

// GoProviderConfig represents provider configuration
type GoProviderConfig struct {
	Type    string
	Version string
}

// RunTemplateConfig represents run template configuration
type RunTemplateConfig struct {
	Provider string
	Storage  string
	Remote   bool
}

// OCPClient aggregates the Kubernetes and OpenShift clients.
type OCPClient struct {
	KubeClient    kubernetes.Interface
	ConfigClient  configv1.ConfigV1Interface
	RouteClient   routev1.RouteV1Interface
	DynamicClient dynamic.Interface
	RESTConfig    *rest.Config
}

// ClusterInfo holds cluster information
type ClusterInfo struct {
	Name       string
	OCPVersion string
	MTVVersion string
	CNVVersion string
	IIB        string
	ConsoleURL string
}

// CmdRunner is a minimal interface for exec commands
// Used for testability in CLI tests
type CmdRunner interface {
	CombinedOutput() ([]byte, error)
	Run() error
}

// Global variables - these need to be in a single file to avoid redeclaration
var (
	ocpClient     *OCPClient
	rootCmd       = &cobra.Command{Use: "mtv-dev", Short: "A CLI for MTV API test development"}
	full          bool
	CLUSTERS_PATH = "/mnt/cnv-qe.rhcloud.com"
	randSrc       = rand.NewSource(time.Now().UnixNano())
	randGen       = rand.New(randSrc)
)

// Provider and storage configurations
var providerMap = map[string]GoProviderConfig{
	"vmware6":   {"vsphere", "6.5"},
	"vmware7":   {"vsphere", "7.0.3"},
	"vmware8":   {"vsphere", "8.0.1"},
	"ovirt":     {"ovirt", "4.4.9"},
	"openstack": {"openstack", "psi"},
	"ova":       {"ova", "nfs"},
}

var storageMap = map[string]string{
	"ceph": "ocs-storagecluster-ceph-rbd",
	"nfs":  "nfs-csi",
	"csi":  "standard-csi",
}

var runsTemplates = map[string]RunTemplateConfig{
	"vmware6-csi":         {"vmware6", "csi", false},
	"vmware6-csi-remote":  {"vmware6", "csi", true},
	"vmware7-ceph":        {"vmware7", "ceph", false},
	"vmware7-ceph-remote": {"vmware7", "ceph", true},
	"vmware8-ceph-remote": {"vmware8", "ceph", true},
	"vmware8-nfs":         {"vmware8", "nfs", false},
	"vmware8-csi":         {"vmware8", "csi", false},
	"openstack-ceph":      {"openstack", "ceph", false},
	"openstack-csi":       {"openstack", "csi", false},
	"ovirt-ceph":          {"ovirt", "ceph", false},
	"ovirt-csi":           {"ovirt", "csi", false},
	"ovirt-csi-remote":    {"ovirt", "csi", true},
	"ova-ceph":            {"ova", "ceph", false},
}
