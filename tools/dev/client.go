package main

import (
	"bytes"
	"context"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	configv1 "github.com/openshift/client-go/config/clientset/versioned/typed/config/v1"
	routev1 "github.com/openshift/client-go/route/clientset/versioned/typed/route/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/client-go/tools/remotecommand"
)

var buildOCPClient = buildOCPClientImpl

func buildOCPClientImpl(clusterName string) (*OCPClient, error) {
	kubeconfigPath := fmt.Sprintf("%s/%s/auth/kubeconfig", CLUSTERS_PATH, clusterName)

	// Check if kubeconfig exists
	if _, err := os.Stat(kubeconfigPath); os.IsNotExist(err) {
		return nil, fmt.Errorf("kubeconfig not found for cluster %s at %s", clusterName, kubeconfigPath)
	}

	var restConfig *rest.Config
	var err error

	// Try in-cluster config first
	restConfig, err = rest.InClusterConfig()
	if err != nil {
		// Get the cluster password
		password, passErr := getClusterPassword(clusterName)
		if passErr != nil {
			return nil, fmt.Errorf("failed to get password for cluster %s: %w", clusterName, passErr)
		}

		// Perform oc login to get a fresh token
		apiURL := fmt.Sprintf("https://api.%s.rhos-psi.cnv-qe.rhood.us:6443", clusterName)
		loginCmd := execCommand("oc", "login", "--insecure-skip-tls-verify=true", apiURL, "-u", "kubeadmin", "-p", password, "--kubeconfig", kubeconfigPath)

		output, loginErr := loginCmd.CombinedOutput()
		if loginErr != nil {
			return nil, fmt.Errorf("failed to login to cluster %s: %w\nOutput: %s", clusterName, loginErr, string(output))
		}

		// Now use the updated kubeconfig file
		restConfig, err = clientcmd.BuildConfigFromFlags("", kubeconfigPath)
		if err != nil {
			return nil, fmt.Errorf("failed to load kubeconfig from %s: %w", kubeconfigPath, err)
		}
	}

	kubeClient, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		return nil, err
	}

	configClient, err := configv1.NewForConfig(restConfig)
	if err != nil {
		return nil, err
	}
	routeClient, err := routev1.NewForConfig(restConfig)
	if err != nil {
		return nil, err
	}
	dynamicClient, err := dynamic.NewForConfig(restConfig)
	if err != nil {
		return nil, err
	}

	return &OCPClient{
		KubeClient:    kubeClient,
		ConfigClient:  configClient,
		RouteClient:   routeClient,
		DynamicClient: dynamicClient,
		RESTConfig:    restConfig,
	}, nil
}

func executeInPod(client *OCPClient, namespace, podName, containerName string, command []string) (string, string, error) {
	// Build query parameters manually to avoid potential parameter encoding issues
	params := url.Values{}
	for _, cmd := range command {
		params.Add("command", cmd)
	}
	if containerName != "" {
		params.Set("container", containerName)
	}
	params.Set("stdout", "true")
	params.Set("stderr", "true")
	params.Set("stdin", "false")

	// Parse the host to get the scheme
	hostURL, err := url.Parse(client.RESTConfig.Host)
	if err != nil {
		return "", "", fmt.Errorf("failed to parse host URL: %w", err)
	}

	executor, err := remotecommand.NewSPDYExecutor(client.RESTConfig, "POST", &url.URL{
		Scheme:   hostURL.Scheme,
		Host:     hostURL.Host,
		Path:     "/api/v1/namespaces/" + namespace + "/pods/" + podName + "/exec",
		RawQuery: params.Encode(),
	})
	if err != nil {
		return "", "", fmt.Errorf("failed to create executor: %w", err)
	}

	var stdout, stderr bytes.Buffer

	streamOptions := remotecommand.StreamOptions{
		Stdout: &stdout,
		Stderr: &stderr,
	}

	err = executor.StreamWithContext(context.Background(), streamOptions)
	if err != nil {
		return stdout.String(), stderr.String(), err
	}
	return stdout.String(), stderr.String(), nil
}

func enableCephTools(clusterName string) (string, error) {
	if err := ensureLoggedIn(clusterName); err != nil {
		return "", fmt.Errorf("failed to login to cluster %s: %w", clusterName, err)
	}

	client, err := buildOCPClient(clusterName)
	if err != nil {
		return "", fmt.Errorf("failed to build OCP client: %w", err)
	}

	// Check if ceph tools are already enabled
	storageClusterGVR := schema.GroupVersionResource{Group: "ocs.openshift.io", Version: "v1", Resource: "storageclusters"}
	storageCluster, err := client.DynamicClient.Resource(storageClusterGVR).Namespace("openshift-storage").Get(context.TODO(), "ocs-storagecluster", metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("failed to get storagecluster: %w", err)
	}

	enableCephTools, found, err := unstructured.NestedBool(storageCluster.Object, "spec", "enableCephTools")
	if err != nil {
		return "", fmt.Errorf("failed to check enableCephTools field: %w", err)
	}

	if !found || !enableCephTools {
		fmt.Fprintf(os.Stderr, "%sEnabling Ceph tools...%s\n", ColorYellow, ColorReset)
		// Patch the storagecluster to enable ceph tools
		patchData := `[{"op": "replace", "path": "/spec/enableCephTools", "value": true}]`
		_, err = client.DynamicClient.Resource(storageClusterGVR).Namespace("openshift-storage").Patch(context.TODO(), "ocs-storagecluster", types.JSONPatchType, []byte(patchData), metav1.PatchOptions{})
		if err != nil {
			return "", fmt.Errorf("failed to enable ceph tools: %w", err)
		}
	}

	fmt.Fprintf(os.Stderr, "%sWaiting for Ceph tools pod...%s\n", ColorYellow, ColorReset)
	// Wait for ceph tools pod to be running (up to 2.5 minutes)
	for i := 0; i < 30; i++ {
		pods, err := client.KubeClient.CoreV1().Pods("openshift-storage").List(context.TODO(), metav1.ListOptions{LabelSelector: "app=rook-ceph-tools"})
		if err == nil && len(pods.Items) > 0 {
			pod := &pods.Items[0]
			if pod.Status.Phase == corev1.PodRunning {
				return pod.Name, nil
			}
		}
		time.Sleep(5 * time.Second)
	}

	return "", fmt.Errorf("timed out waiting for Ceph tools pod to become ready")
}

var getClusterInfo = getClusterInfoImpl

func getClusterInfoImpl(clusterName string) (*ClusterInfo, error) {
	client, err := buildOCPClient(clusterName)
	if err != nil {
		return nil, fmt.Errorf("could not connect to %s: %w", clusterName, err)
	}

	info := &ClusterInfo{Name: clusterName}

	// OCP Version with better error handling
	ocpVer, err := client.ConfigClient.ClusterVersions().Get(context.TODO(), "version", metav1.GetOptions{})
	if err != nil {
		// If we can't get cluster version, try to get it from server version as fallback
		serverVersion, serverErr := client.KubeClient.Discovery().ServerVersion()
		if serverErr == nil {
			info.OCPVersion = serverVersion.GitVersion
		}
	} else {
		// Find the completed version from history
		for _, history := range ocpVer.Status.History {
			if history.State == "Completed" {
				info.OCPVersion = history.Version
				break
			}
		}
		// If no completed version found, try the desired version
		if info.OCPVersion == "" && len(ocpVer.Status.Desired.Version) > 0 {
			info.OCPVersion = ocpVer.Status.Desired.Version
		}
	}

	// MTV Version with improved error handling
	csvGVR := schema.GroupVersionResource{Group: "operators.coreos.com", Version: "v1alpha1", Resource: "clusterserviceversions"}
	mtvCSVs, err := client.DynamicClient.Resource(csvGVR).Namespace("openshift-mtv").List(context.TODO(), metav1.ListOptions{})
	if err != nil {
		info.MTVVersion = "Not installed"
	} else {
		mtvFound := false
		for _, item := range mtvCSVs.Items {
			replacedBy, hasReplacedBy, _ := unstructured.NestedString(item.Object, "status", "replacedBy")
			version, hasVersion, _ := unstructured.NestedString(item.Object, "spec", "version")

			// Look for active CSVs (not replaced)
			if !hasReplacedBy || replacedBy == "" {
				if hasVersion && version != "" {
					info.MTVVersion = version
					mtvFound = true
					break
				}
			}
		}
		if !mtvFound {
			info.MTVVersion = "Not installed"
		}
	}

	// CNV Version with improved error handling
	cnvCSVs, err := client.DynamicClient.Resource(csvGVR).Namespace("openshift-cnv").List(context.TODO(), metav1.ListOptions{})
	if err != nil {
		info.CNVVersion = "Not installed"
	} else {
		cnvFound := false
		for _, item := range cnvCSVs.Items {
			replacedBy, hasReplacedBy, _ := unstructured.NestedString(item.Object, "status", "replacedBy")
			version, hasVersion, _ := unstructured.NestedString(item.Object, "spec", "version")

			// Look for active CSVs (not replaced)
			if !hasReplacedBy || replacedBy == "" {
				if hasVersion && version != "" {
					info.CNVVersion = version
					cnvFound = true
					break
				}
			}
		}
		if !cnvFound {
			info.CNVVersion = "Not installed"
		}
	}

	// IIB with improved filtering and error handling
	csGVR := schema.GroupVersionResource{Group: "operators.coreos.com", Version: "v1alpha1", Resource: "catalogsources"}
	catalogSources, err := client.DynamicClient.Resource(csGVR).Namespace("openshift-marketplace").List(context.TODO(), metav1.ListOptions{})
	if err == nil {
		var filteredSources []unstructured.Unstructured
		for _, item := range catalogSources.Items {
			name := item.GetName()
			// Look for MTV-related IIB sources
			if strings.HasPrefix(name, "iib-") ||
				strings.Contains(name, "redhat-osbs-") ||
				strings.Contains(name, "mtv") ||
				strings.Contains(name, "forklift") {
				filteredSources = append(filteredSources, item)
			}
		}
		// Sort by creation time (newest first)
		sort.Slice(filteredSources, func(i, j int) bool {
			return filteredSources[i].GetCreationTimestamp().After(filteredSources[j].GetCreationTimestamp().Time)
		})
		if len(filteredSources) > 0 {
			info.IIB = filteredSources[0].GetName()
		}
	}

	// Console URL with error handling
	console, err := client.RouteClient.Routes("openshift-console").Get(context.TODO(), "console", metav1.GetOptions{})
	if err == nil {
		info.ConsoleURL = "https://" + console.Spec.Host
	} else {
		// Fallback console URL based on cluster name pattern
		info.ConsoleURL = fmt.Sprintf("https://console-openshift-console.apps.%s.rhos-psi.cnv-qe.rhood.us", clusterName)
	}

	// Set default values for IIB based on MTV installation status
	if info.IIB == "" {
		if info.MTVVersion != "Not installed" {
			info.IIB = "Unknown"
		} else {
			info.IIB = "N/A"
		}
	}

	return info, nil
}
