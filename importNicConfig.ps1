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

    # Use netsh to set static IP — this atomically disables DHCP and assigns
    # the address in one step, avoiding the "Inconsistent parameters
    # PolicyStore PersistentStore and Dhcp Enabled" error from New-NetIPAddress.
    $prefixLen = [int]$config.prefixLength
    $maskInt = [uint32]([math]::Pow(2, 32) - [math]::Pow(2, 32 - $prefixLen))
    $subnetMask = "{0}.{1}.{2}.{3}" -f (($maskInt -shr 24) -band 0xFF), (($maskInt -shr 16) -band 0xFF), (($maskInt -shr 8) -band 0xFF), ($maskInt -band 0xFF)

    if ($config.defaultGateway) {
        netsh interface ip set address name="$alias" static $($config.ipv4Address) $subnetMask $($config.defaultGateway)
    } else {
        netsh interface ip set address name="$alias" static $($config.ipv4Address) $subnetMask
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "netsh set address failed for adapter '$alias' (exit code $LASTEXITCODE)"
        exit 1
    }

    # Set DNS
    if ($config.dnsServers) {
        Set-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -ServerAddresses ($config.dnsServers -split ",")
    }
}

Write-Host "Import complete."
