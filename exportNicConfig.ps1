$exportPath = "C:\TMP\pveMigration\network.json"

$exportDir = Split-Path -Path $exportPath -Parent
if (-Not (Test-Path $exportDir)) {
    New-Item -ItemType Directory -Path $exportDir -Force | Out-Null
}

$networkConfigs = @()
$nicIndex = 0

# Get all physical/virtual adapters sorted by InterfaceIndex (matches NIC creation order).
# Then look up IP configuration for each adapter that has one.
$adapters = Get-NetAdapter | Where-Object { $_.Status -eq "Up" } | Sort-Object InterfaceIndex

foreach ($adapter in $adapters) {
    $ipConfig = Get-NetIPConfiguration -InterfaceIndex $adapter.InterfaceIndex -ErrorAction SilentlyContinue
    if ($null -eq $ipConfig -or $null -eq $ipConfig.IPv4Address) {
        continue
    }

    $config = [PSCustomObject]@{
        nicIndex         = $nicIndex
        interfaceAlias   = $adapter.Name
        ipv4Address      = $ipConfig.IPv4Address.IPAddress
        prefixLength     = $ipConfig.IPv4Address.PrefixLength
        defaultGateway   = $ipConfig.IPv4DefaultGateway.NextHop
        dnsServers       = ($ipConfig.DnsServer.ServerAddresses -join ",")
    }

    $networkConfigs += $config
    $nicIndex++
}

$networkConfigs | ConvertTo-Json -Depth 5 | Out-File -Encoding UTF8 $exportPath

Write-Host "Network configuration exported to $exportPath"