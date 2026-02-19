$importPath = "C:\TMP\pveMigration\network.json"

if (-Not (Test-Path $importPath)) {
    Write-Error "File $importPath was not found."
    exit 1
}

$configs = Get-Content $importPath | ConvertFrom-Json

# @() ensures the result is always an array, even with a single entry
$configs = @($configs | Sort-Object nicIndex)

# Get all adapters sorted by InterfaceIndex (matches NIC creation order in Proxmox).
# @() ensures the result is always an array, even with a single adapter.
$adapters = @(Get-NetAdapter | Sort-Object InterfaceIndex)

Write-Host "Found $($configs.Count) saved NIC config(s) and $($adapters.Count) adapter(s)."

# Apply configs positionally: config[0] -> adapter[0], config[1] -> adapter[1], etc.
# The nicIndex in the export is sequential (0, 1, 2...) matching vCenter NIC order.
# Proxmox creates NICs in the same order, so adapter[i] by InterfaceIndex matches.
for ($i = 0; $i -lt $configs.Count; $i++) {
    $config = $configs[$i]

    if ($i -ge $adapters.Count) {
        Write-Warning "No adapter found for NIC position $i – skipping."
        continue
    }

    $adapter = $adapters[$i]
    $alias = $adapter.Name

    Write-Host "Applying config $i to '$alias' (ifIndex $($adapter.InterfaceIndex))..."
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
