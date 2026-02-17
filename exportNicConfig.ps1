$exportPath = "C:\TMP\pveMigration\network.json"

$exportDir = Split-Path -Path $exportPath -Parent
if (-Not (Test-Path $exportDir)) {
    New-Item -ItemType Directory -Path $exportDir -Force | Out-Null
}

$networkConfigs = @()
$nicIndex = 0

# Only consider adapters with a valid IPv4 address
$adapters = Get-NetIPConfiguration | Where-Object { $_.IPv4Address -ne $null }

foreach ($adapter in $adapters) {
    $config = [PSCustomObject]@{
        nicIndex         = $nicIndex
        interfaceAlias   = $adapter.InterfaceAlias
        ipv4Address      = $adapter.IPv4Address.IPAddress
        prefixLength     = $adapter.IPv4Address.PrefixLength
        defaultGateway   = $adapter.IPv4DefaultGateway.NextHop
        dnsServers       = ($adapter.DnsServer.ServerAddresses -join ",")
    }

    $networkConfigs += $config
    $nicIndex++
}

$networkConfigs | ConvertTo-Json -Depth 5 | Out-File -Encoding UTF8 $exportPath

Write-Host "Network configuration exported to $exportPath"