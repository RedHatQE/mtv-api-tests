package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gophercloud/gophercloud/v2"
	"github.com/gophercloud/gophercloud/v2/openstack"
	"github.com/gophercloud/gophercloud/v2/openstack/compute/v2/servers"
	ovirtclientlog "github.com/ovirt/go-ovirt-client-log/v3"
	ovirtclient "github.com/ovirt/go-ovirt-client/v3"
	"github.com/vmware/govmomi"
	"github.com/vmware/govmomi/find"
	"github.com/vmware/govmomi/vim25/mo"
	"github.com/vmware/govmomi/vim25/soap"
	"github.com/vmware/govmomi/vim25/types"
)

// VMInfo represents basic information about a virtual machine
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

// VMProvider interface for different virtualization platforms
type VMProvider interface {
	Connect() error
	ListVMs(ctx context.Context) ([]VMInfo, error)
	GetVM(ctx context.Context, name string) (*VMInfo, error)
	Close() error
	GetProviderType() string
}

// VMwareProvider implements VMProvider for VMware vSphere
type VMwareProvider struct {
	URL      string
	Username string
	Password string
	Insecure bool
	client   *govmomi.Client
}

// NewVMwareProvider creates a new VMware provider
func NewVMwareProvider(url, username, password string, insecure bool) *VMwareProvider {
	return &VMwareProvider{
		URL:      url,
		Username: username,
		Password: password,
		Insecure: insecure,
	}
}

func (p *VMwareProvider) Connect() error {
	ctx := context.Background()

	u, err := soap.ParseURL(p.URL)
	if err != nil {
		return fmt.Errorf("failed to parse VMware URL: %w", err)
	}

	u.User = url.UserPassword(p.Username, p.Password)

	client, err := govmomi.NewClient(ctx, u, p.Insecure)
	if err != nil {
		return fmt.Errorf("failed to create VMware client: %w", err)
	}

	p.client = client
	return nil
}

func (p *VMwareProvider) ListVMs(ctx context.Context) ([]VMInfo, error) {
	if p.client == nil {
		return nil, fmt.Errorf("not connected to VMware")
	}

	finder := find.NewFinder(p.client.Client, true)

	// Find all datacenters
	datacenters, err := finder.DatacenterList(ctx, "*")
	if err != nil {
		return nil, fmt.Errorf("failed to find datacenters: %w", err)
	}

	var allVMs []VMInfo

	for _, dc := range datacenters {
		finder.SetDatacenter(dc)

		// Find all VMs in this datacenter
		vms, err := finder.VirtualMachineList(ctx, "*")
		if err != nil {
			continue // Skip datacenters where we can't list VMs
		}

		// Convert VM objects to ManagedObjectReference for Retrieve
		var vmRefs []types.ManagedObjectReference
		for _, vm := range vms {
			vmRefs = append(vmRefs, vm.Reference())
		}

		// Get VM properties
		var vmObjects []mo.VirtualMachine
		err = p.client.Retrieve(ctx, vmRefs, []string{"name", "runtime.powerState", "config.uuid", "config.hardware.numCPU", "config.hardware.memoryMB", "config.guestFullName", "guest.ipAddress", "guest.net", "storage", "config.createDate", "config.modified", "tag", "runtime.host", "resourcePool", "network"}, &vmObjects)
		if err != nil {
			continue // Skip on error
		}

		for _, vm := range vmObjects {
			vmInfo := VMInfo{
				Name:         vm.Name,
				Provider:     "vmware",
				PowerState:   string(vm.Runtime.PowerState),
				UUID:         vm.Config.Uuid,
				CPU:          vm.Config.Hardware.NumCPU,
				MemoryMB:     int64(vm.Config.Hardware.MemoryMB),
				GuestOS:      vm.Config.GuestFullName,
				CreationTime: vm.Config.CreateDate,
			}

			// Get IP addresses
			if vm.Guest != nil && vm.Guest.IpAddress != "" {
				vmInfo.IPAddresses = append(vmInfo.IPAddresses, vm.Guest.IpAddress)
			}

			// Get network interfaces
			if vm.Guest != nil && vm.Guest.Net != nil {
				for _, net := range vm.Guest.Net {
					if net.IpAddress != nil {
						vmInfo.IPAddresses = append(vmInfo.IPAddresses, net.IpAddress...)
					}
					if net.Network != "" {
						vmInfo.Networks = append(vmInfo.Networks, net.Network)
					}
				}
			}

			// Convert storage from bytes to GB
			if vm.Storage != nil && vm.Storage.PerDatastoreUsage != nil {
				var totalStorage int64
				for _, usage := range vm.Storage.PerDatastoreUsage {
					totalStorage += usage.Committed
				}
				vmInfo.StorageGB = float64(totalStorage) / (1024 * 1024 * 1024)
			}

			allVMs = append(allVMs, vmInfo)
		}
	}

	return allVMs, nil
}

func (p *VMwareProvider) GetVM(ctx context.Context, name string) (*VMInfo, error) {
	vms, err := p.ListVMs(ctx)
	if err != nil {
		return nil, err
	}

	for _, vm := range vms {
		if vm.Name == name {
			return &vm, nil
		}
	}

	return nil, fmt.Errorf("VM '%s' not found", name)
}

func (p *VMwareProvider) Close() error {
	if p.client != nil {
		return p.client.Logout(context.Background())
	}
	return nil
}

func (p *VMwareProvider) GetProviderType() string {
	return "vmware"
}

// oVirtProvider implements VMProvider for oVirt/RHV
type oVirtProvider struct {
	URL      string
	Username string
	Password string
	Insecure bool
	client   ovirtclient.Client
}

// NewOVirtProvider creates a new oVirt provider
func NewOVirtProvider(url, username, password string, insecure bool) *oVirtProvider {
	return &oVirtProvider{
		URL:      url,
		Username: username,
		Password: password,
		Insecure: insecure,
	}
}

func (p *oVirtProvider) Connect() error {
	logger := ovirtclientlog.NewNOOPLogger()

	var tls ovirtclient.TLSProvider
	if p.Insecure {
		tls = ovirtclient.TLS().Insecure()
	} else {
		tls = ovirtclient.TLS().CACertsFromSystem()
	}

	client, err := ovirtclient.New(
		p.URL,
		p.Username,
		p.Password,
		tls,
		logger,
		nil,
	)
	if err != nil {
		return fmt.Errorf("failed to create oVirt client: %w", err)
	}

	p.client = client
	return nil
}

func (p *oVirtProvider) ListVMs(ctx context.Context) ([]VMInfo, error) {
	if p.client == nil {
		return nil, fmt.Errorf("not connected to oVirt")
	}

	vms, err := p.client.ListVMs()
	if err != nil {
		return nil, fmt.Errorf("failed to list VMs: %w", err)
	}

	var vmInfos []VMInfo
	for _, vm := range vms {
		// Map oVirt VM status to standard power states
		// TODO: Many VMs (251 out of ~1930) still show as "unknown" status in oVirt.
		// This needs further investigation - may be related to:
		// 1. VMs in transitional states
		// 2. VMs with missing or corrupt status information
		// 3. VMs that require additional API calls to get detailed status
		// 4. Specific oVirt client library limitations
		// Consider investigating oVirt API directly or using different status retrieval methods
		var powerState string
		switch vm.Status() {
		case ovirtclient.VMStatusUp:
			powerState = "running"
		case ovirtclient.VMStatusDown:
			powerState = "poweredoff"
		case ovirtclient.VMStatusPoweringUp:
			powerState = "powering_on"
		case ovirtclient.VMStatusPoweringDown:
			powerState = "powering_off"
		case ovirtclient.VMStatusSuspended:
			powerState = "suspended"
		case ovirtclient.VMStatusImageLocked:
			powerState = "locked"
		case ovirtclient.VMStatusMigrating:
			powerState = "migrating"
		case ovirtclient.VMStatusUnknown:
			powerState = "unknown"
		case ovirtclient.VMStatusNotResponding:
			powerState = "not_responding"
		case ovirtclient.VMStatusWaitForLaunch:
			powerState = "waiting"
		case ovirtclient.VMStatusSavingState:
			powerState = "saving"
		case ovirtclient.VMStatusRestoringState:
			powerState = "restoring"
		default:
			powerState = "unknown"
		}

		vmInfo := VMInfo{
			Name:       vm.Name(),
			Provider:   "ovirt",
			PowerState: powerState,
			UUID:       string(vm.ID()),
			MemoryMB:   vm.Memory() / (1024 * 1024),
		}

		// Get CPU information - simplified approach
		if vmCPU := vm.CPU(); vmCPU != nil {
			// Use a default of 2 CPU cores if CPU info is available but detailed info isn't accessible
			vmInfo.CPU = int32(2)
		} else {
			vmInfo.CPU = 1 // default if CPU info is not available
		}

		// Get OS information
		if vmOS := vm.OS(); vmOS != nil {
			vmInfo.GuestOS = vmOS.Type()
		}

		vmInfos = append(vmInfos, vmInfo)
	}

	return vmInfos, nil
}

func (p *oVirtProvider) GetVM(ctx context.Context, name string) (*VMInfo, error) {
	vms, err := p.ListVMs(ctx)
	if err != nil {
		return nil, err
	}

	for _, vm := range vms {
		if vm.Name == name {
			return &vm, nil
		}
	}

	return nil, fmt.Errorf("VM '%s' not found", name)
}

func (p *oVirtProvider) Close() error {
	// oVirt client doesn't need explicit closing
	return nil
}

func (p *oVirtProvider) GetProviderType() string {
	return "ovirt"
}

// OpenStackProvider implements VMProvider for OpenStack
type OpenStackProvider struct {
	AuthURL         string
	Username        string
	Password        string
	TenantName      string
	Region          string
	UserDomainName  string
	UserDomainID    string
	ProjectDomainID string
	Insecure        bool
	client          *gophercloud.ServiceClient
}

// NewOpenStackProvider creates a new OpenStack provider
func NewOpenStackProvider(authURL, username, password, tenantName, region, userDomainName, userDomainID, projectDomainID string, insecure bool) *OpenStackProvider {
	return &OpenStackProvider{
		AuthURL:         authURL,
		Username:        username,
		Password:        password,
		TenantName:      tenantName,
		Region:          region,
		UserDomainName:  userDomainName,
		UserDomainID:    userDomainID,
		ProjectDomainID: projectDomainID,
		Insecure:        insecure,
	}
}

func (p *OpenStackProvider) Connect() error {
	opts := gophercloud.AuthOptions{
		IdentityEndpoint: p.AuthURL,
		Username:         p.Username,
		Password:         p.Password,
		TenantName:       p.TenantName,
		AllowReauth:      true,
	}

	// Add domain authentication - prefer DomainID over DomainName
	if p.UserDomainID != "" {
		opts.DomainID = p.UserDomainID
	} else if p.UserDomainName != "" {
		opts.DomainName = p.UserDomainName
	}

	// Set project domain if available
	if p.ProjectDomainID != "" {
		opts.Scope = &gophercloud.AuthScope{
			ProjectName: p.TenantName,
			DomainID:    p.ProjectDomainID,
		}
		// Clear TenantName when using Scope
		opts.TenantName = ""
	}

	ctx := context.Background()
	provider, err := openstack.AuthenticatedClient(ctx, opts)
	if err != nil {
		return fmt.Errorf("failed to authenticate with OpenStack: %w", err)
	}

	// Configure TLS if insecure
	if p.Insecure {
		provider.HTTPClient.Transport = &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
		}
	}

	endpointOpts := gophercloud.EndpointOpts{
		Region: p.Region,
	}

	client, err := openstack.NewComputeV2(provider, endpointOpts)
	if err != nil {
		return fmt.Errorf("failed to create compute client: %w", err)
	}

	p.client = client
	return nil
}

func (p *OpenStackProvider) ListVMs(ctx context.Context) ([]VMInfo, error) {
	if p.client == nil {
		return nil, fmt.Errorf("not connected to OpenStack")
	}

	listOpts := servers.ListOpts{
		AllTenants: false,
	}

	allPages, err := servers.List(p.client, listOpts).AllPages(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to list servers: %w", err)
	}

	serverList, err := servers.ExtractServers(allPages)
	if err != nil {
		return nil, fmt.Errorf("failed to extract servers: %w", err)
	}

	var vmInfos []VMInfo
	for _, server := range serverList {
		vmInfo := VMInfo{
			Name:         server.Name,
			Provider:     "openstack",
			PowerState:   server.Status,
			UUID:         server.ID,
			CreationTime: &server.Created,
			LastModified: &server.Updated,
		}

		// Safely extract image name if available
		if server.Image != nil {
			if name, ok := server.Image["name"].(string); ok {
				vmInfo.GuestOS = name
			}
		}

		// Get IP addresses from all networks
		for _, network := range server.Addresses {
			if addrs, ok := network.([]interface{}); ok {
				for _, addr := range addrs {
					if addrMap, ok := addr.(map[string]interface{}); ok {
						if ip, ok := addrMap["addr"].(string); ok {
							vmInfo.IPAddresses = append(vmInfo.IPAddresses, ip)
						}
					}
				}
			}
		}

		// Get flavor information for CPU and memory if available
		if server.Flavor["id"] != nil {
			// Note: Getting detailed flavor info would require additional API call
			// For now, we'll just note that it's available
			vmInfo.Tags = append(vmInfo.Tags, fmt.Sprintf("flavor:%s", server.Flavor["id"]))
		}

		vmInfos = append(vmInfos, vmInfo)
	}

	return vmInfos, nil
}

func (p *OpenStackProvider) GetVM(ctx context.Context, name string) (*VMInfo, error) {
	vms, err := p.ListVMs(ctx)
	if err != nil {
		return nil, err
	}

	for _, vm := range vms {
		if vm.Name == name {
			return &vm, nil
		}
	}

	return nil, fmt.Errorf("VM '%s' not found", name)
}

func (p *OpenStackProvider) Close() error {
	// OpenStack client doesn't need explicit closing
	return nil
}

func (p *OpenStackProvider) GetProviderType() string {
	return "openstack"
}

// CreateProvider creates a provider based on configuration
func CreateProvider(providerType, url, username, password string, insecure bool, extraParams map[string]string) (VMProvider, error) {
	switch strings.ToLower(providerType) {
	case "vmware", "vsphere":
		return NewVMwareProvider(url, username, password, insecure), nil
	case "ovirt", "rhv":
		return NewOVirtProvider(url, username, password, insecure), nil
	case "openstack":
		tenantName := extraParams["tenant_name"]
		region := extraParams["region"]
		if region == "" {
			region = "regionOne" // default region
		}
		userDomainName := extraParams["user_domain_name"]
		userDomainID := extraParams["user_domain_id"]
		projectDomainID := extraParams["project_domain_id"]
		return NewOpenStackProvider(url, username, password, tenantName, region, userDomainName, userDomainID, projectDomainID, insecure), nil
	default:
		return nil, fmt.Errorf("unsupported provider type: %s", providerType)
	}
}
