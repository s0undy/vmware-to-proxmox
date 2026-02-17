$importPath = "C:\TMP\pveMigration\network.json"

$importDir = Split-Path -Path $importPath -Parent
if (-Not (Test-Path $importDir)) {
    New-Item -ItemType Directory -Path $importDir -Force | Out-Null
}

if (-Not (Test-Path $importPath)) {
    Write-Error "File $importPath was not found."
    exit
}

$configs = Get-Content $importPath | ConvertFrom-Json

# Get all adapters sorted by InterfaceIndex to produce a stable ordering
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

    # Clear old settings (optional)
    try {
        Remove-NetIPAddress -InterfaceAlias $alias -Confirm:$false -ErrorAction SilentlyContinue
        Remove-NetRoute -InterfaceAlias $alias -Confirm:$false -ErrorAction SilentlyContinue
    } catch {}

    # Set new IP
    New-NetIPAddress -InterfaceAlias $alias -IPAddress $config.ipv4Address -PrefixLength $config.prefixLength -DefaultGateway $config.defaultGateway

    # Set DNS
    Set-DnsClientServerAddress -InterfaceAlias $alias -ServerAddresses ($config.dnsServers -split ",")
}

Write-Host "Import complete."