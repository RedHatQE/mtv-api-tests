## written originlly by Tzahi Ashkenaz
## $ sudo yum install https://github.com/PowerShell/PowerShell/releases/download/v7.1.2/powershell-7.1.2-1.rhel.7.x86_64.rpm
## $  pwsh
## $  install-module vmware.powercli

 param (

    [Parameter(Mandatory=$true)][string]$vmname,
    [Parameter(Mandatory=$true)][string]$server

 )



Set-PowerCLIConfiguration -InvalidCertificateAction:Ignore
Connect-VIServer  -Server $server

Get-VM -Name $vmname  | Get-View |  Select Name, @{N="ChangeTrackingStatus";E={$_.Config.ChangeTrackingEnabled}}

$targets = Get-VM  -name $vmname | Select Name, @{N="CBT";E={(Get-View $_).Config.ChangeTrackingEnabled}} | WHERE {$_.CBT -like "False"}
ForEach ($target in $targets) {
 $vm = $target.Name
 Get-VM $vm | Get-Snapshot | Remove-Snapshot -confirm:$false
 $vmView = Get-vm $vm | get-view
 $vmConfigSpec = New-Object VMware.Vim.VirtualMachineConfigSpec
 $vmConfigSpec.changeTrackingEnabled = $true
 $vmView.reconfigVM($vmConfigSpec)
 New-Snapshot -VM (Get-VM $vm ) -Name "CBTSnap"
 Get-VM $vm | Get-Snapshot -Name "CBTSnap" | Remove-Snapshot -confirm:$false
}
