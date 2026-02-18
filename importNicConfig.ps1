$importPath = "C:\TMP\pveMigration\network.json"

if (-Not (Test-Path $importPath)) {
    Write-Error "File $importPath was not found."
    exit 1
}

$configs = Get-Content $importPath | ConvertFrom-Json

# Sort saved configs by nicIndex to ensure positional ordering
$configs = $configs | Sort-Object nicIndex

# Get all adapters sorted by InterfaceIndex (matches NIC creation order in Proxmox).
# The positional index here corresponds to the nicIndex from the export —
# both VMware and Proxmox create NICs in the same order.
$adapters = Get-NetAdapter | Sort-Object InterfaceIndex

foreach ($config in $configs) {
    $idx = $config.nicIndex

    if ($idx -ge $adapters.Count) {
        Write-Warning "No adapter found for nicIndex $idx – skipping."
        continue
    }

    $adapter = $adapters[$idx]
    $alias = $adapter.Name

    Write-Host "Applying configuration to $alias (nicIndex $idx)..."
    Write-Host "  IP: $($config.ipv4Address)/$($config.prefixLength)  GW: $($config.defaultGateway)  DNS: $($config.dnsServers)"

    # Clear existing IP addresses and routes
    try {
        Remove-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -Confirm:$false -ErrorAction SilentlyContinue
        Remove-NetRoute -InterfaceIndex $adapter.InterfaceIndex -Confirm:$false -ErrorAction SilentlyContinue
    } catch {}

    # Set new IP
    $ipParams = @{
        InterfaceIndex = $adapter.InterfaceIndex
        IPAddress      = $config.ipv4Address
        PrefixLength   = $config.prefixLength
    }
    if ($config.defaultGateway) {
        $ipParams["DefaultGateway"] = $config.defaultGateway
    }
    New-NetIPAddress @ipParams

    # Set DNS
    if ($config.dnsServers) {
        Set-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -ServerAddresses ($config.dnsServers -split ",")
    }
}

Write-Host "Import complete."
