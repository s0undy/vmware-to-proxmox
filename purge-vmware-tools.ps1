<#
.SYNOPSIS
    Uninstalls and removes VMware Tools from a Windows system.
.DESCRIPTION
    This script first attempts to uninstall VMware Tools using the MSI installer method,
    even if the system is no longer running on VMware. It then performs a comprehensive
    cleanup by removing registry entries, filesystem folders, services, and devices.

    WARNING: If running this script on a system that has been migrated to a different
    hypervisor (e.g., Proxmox with VirtIO drivers), removing VMware storage drivers may
    cause boot failures. In such cases:
    - Ensure VirtIO drivers (or equivalent) are installed BEFORE running this script
    - If boot issues occur, change the disk controller type to IDE or SATA in the
      hypervisor settings, boot the system, then reinstall the appropriate drivers
    - Consider taking a snapshot before running this script if on a virtualized system

.PARAMETER Force
    Bypass the confirmation prompt and proceed with uninstall and cleanup automatically.
.PARAMETER Reboot
    Reboot the system after cleanup completes. If -Force is not specified, prompts for confirmation.
.EXAMPLE
    .\Cleanup-VMwareTools.ps1
    Prompts for confirmation before uninstalling and removing VMware Tools.
.EXAMPLE
    .\Cleanup-VMwareTools.ps1 -Force
    Uninstalls and removes VMware Tools without prompting for confirmation.
.EXAMPLE
    .\Cleanup-VMwareTools.ps1 -Force -Reboot
    Uninstalls and removes VMware Tools, then reboots automatically without prompting.
.EXAMPLE
    .\Cleanup-VMwareTools.ps1 -Reboot
    Uninstalls and removes VMware Tools with prompts, then asks before rebooting.
.NOTES
    This script combines techniques from two sources:
    - MSI uninstaller method: https://gist.github.com/KGHague/2c562ee88492c1c0c0eac1b3ae0fecd8
    - Brute-force cleanup method: https://gist.github.com/broestls/f872872a00acee2fca02017160840624
.LINK
    https://gist.github.com/KGHague/2c562ee88492c1c0c0eac1b3ae0fecd8
.LINK
    https://gist.github.com/broestls/f872872a00acee2fca02017160840624
#>

[CmdletBinding()]
Param (
    [Parameter(Mandatory=$false)]
    [switch]$Force,

    [Parameter(Mandatory=$false)]
    [switch]$Reboot
)

#Requires -RunAsAdministrator

#region Transcript and Logging Setup
# Start transcript with datestamped filename in script directory
$scriptName     = [System.IO.Path]::GetFileNameWithoutExtension($MyInvocation.MyCommand.Name)
$transcriptPath = Join-Path $PSScriptRoot "${scriptName}_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
Start-Transcript -Path $transcriptPath -Append
Write-Host "Transcript started: $transcriptPath" -ForegroundColor Cyan
Write-Host "Script started at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan

# Initialize script timer
$scriptStartTime = Get-Date
#endregion

#region Helper Functions
function Get-VMwareToolsInstallerID {
    <#
    .SYNOPSIS
        Retrieves the common ID used for VMware registry entries along with the MSI ID.
    .DESCRIPTION
        This function pulls out the common ID used for most of the VMware registry entries
        along with the ID associated with the MSI for VMware Tools.
    #>
    foreach ($item in $(Get-ChildItem Registry::HKEY_CLASSES_ROOT\Installer\Products)) {
        if ($item.GetValue('ProductName') -eq 'VMware Tools') {
            return @{
                reg_id = $item.PSChildName;
                msi_id = [Regex]::Match($item.GetValue('ProductIcon'), '(?<={)(.*?)(?=})') | Select-Object -ExpandProperty Value
            }
        }
    }
}
#endregion

#region Gather VMware Tools Information
$stepStartTime = Get-Date
Write-Host "`n=== Gathering VMware Tools Information ===" -ForegroundColor Cyan

# Get VMware Tools installer IDs before attempting uninstallation
# This ensures we have the registry IDs even if MSI uninstall removes them
$vmware_tools_ids = Get-VMwareToolsInstallerID

if ($vmware_tools_ids) {
    Write-Host "VMware Tools installer IDs found:" -ForegroundColor Green
    Write-Host "  Registry ID: $($vmware_tools_ids.reg_id)" -ForegroundColor Gray
    Write-Host "  MSI ID: $($vmware_tools_ids.msi_id)" -ForegroundColor Gray
}
else {
    Write-Host "VMware Tools installer IDs not found in registry." -ForegroundColor Yellow
}

$stepDuration = (Get-Date) - $stepStartTime
Write-Host "Step completed in $($stepDuration.TotalSeconds.ToString('F2')) seconds" -ForegroundColor Gray
#endregion

#region Step 1: MSI-based Uninstallation
$stepStartTime = Get-Date
Write-Host "`n=== Step 1: Attempting MSI-based Uninstallation ===" -ForegroundColor Cyan

# Create an instance of the WindowsInstaller.Installer object
$installer = New-Object -ComObject WindowsInstaller.Installer

# Use the packed GUID we already found earlier
if ($vmware_tools_ids) {
    Write-Host "VMware Tools installation found via registry." -ForegroundColor Green

    # Get the LocalPackage path from the registry using the packed GUID we already have
    $localPackage = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products\$($vmware_tools_ids.reg_id)\InstallProperties" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPackage

    if ($localPackage) {
        Write-Host "VMware Tools MSI path: $localPackage" -ForegroundColor Yellow

        # Open the MSI database in read-write mode
        $database = $installer.GetType().InvokeMember("OpenDatabase", "InvokeMethod", $null, $installer, @("${localPackage}", 2))

        # Remove the VM_LogStart and VM_CheckRequirements rows in the CustomAction table
        # VM_CheckRequirements added as recommended by @DanAvni
        $query = "DELETE FROM CustomAction WHERE Action='VM_LogStart' OR Action='VM_CheckRequirements'"
        $view  = $database.GetType().InvokeMember("OpenView", "InvokeMethod", $null, $database, @($query))
        $view.GetType().InvokeMember("Execute", "InvokeMethod", $null, $view, $null)
        $view.GetType().InvokeMember("Close", "InvokeMethod", $null, $view, $null)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($view)

        # Commit the changes and close the database
        $database.GetType().InvokeMember("Commit", "InvokeMethod", $null, $database, $null)
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($database)

        Write-Host "MSI database modified successfully." -ForegroundColor Green

        # Check if Force parameter is used or get user confirmation
        if ($Force) {
            $user_confirmed = "y"
            Write-Host "Force parameter specified - proceeding with MSI uninstallation..." -ForegroundColor Yellow
        }
        else {
            $user_confirmed = Read-Host "Proceed with MSI uninstallation? (y/n)"
        }

        if ($user_confirmed -eq "y") {
            Write-Host "Uninstalling VMware Tools via MSI..." -ForegroundColor Yellow
            Start-Process msiexec.exe -ArgumentList "/x `"${localPackage}`" /qn /norestart" -Wait
            Write-Host "MSI uninstallation completed." -ForegroundColor Green
        }
        else {
            Write-Host "MSI uninstallation skipped by user." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "LocalPackage path not found in the registry." -ForegroundColor Yellow
    }
}
else {
    Write-Host "VMware Tools is not installed via MSI or not found in Win32_Product." -ForegroundColor Yellow
}

$stepDuration = (Get-Date) - $stepStartTime
Write-Host "Step completed in $($stepDuration.TotalSeconds.ToString('F2')) seconds" -ForegroundColor Gray
#endregion

#region Step 2: Comprehensive Cleanup
$stepStartTime = Get-Date
Write-Host "`n=== Step 2: Comprehensive Cleanup ===" -ForegroundColor Cyan

# Use the VMware Tools IDs gathered earlier
# Targets we can hit with the common registry ID from $vmware_tools_ids.reg_id
$reg_targets = @(
    "Registry::HKEY_CLASSES_ROOT\Installer\Features\",
    "Registry::HKEY_CLASSES_ROOT\Installer\Products\",
    "HKLM:\SOFTWARE\Classes\Installer\Features\",
    "HKLM:\SOFTWARE\Classes\Installer\Products\",
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products\"
)

$VMware_Tools_Directory       = "${env:SystemDrive}\Program Files\VMware"
$VMware_Common_Directory      = "${env:SystemDrive}\Program Files\Common Files\VMware"
$VMware_Startmenu_Entry       = "${env:SystemDrive}\ProgramData\Microsoft\Windows\Start Menu\Programs\VMware\"
$VMware_ProgramData_Directory = "${env:SystemDrive}\ProgramData\VMware"

# Create an empty array to hold all the uninstallation targets and compose the entries into the target array
$targets = @()

if ($vmware_tools_ids) {
    foreach ($item in $reg_targets) {
        $targets += $item + $vmware_tools_ids.reg_id
    }
    # Add the MSI installer ID regkey
    $targets += "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{$($vmware_tools_ids.msi_id)}"
}

# This is a bit of a shotgun approach, but if we are at a version less than 2016, add the Uninstaller entries we don't
# try to automatically determine.
if ([Environment]::OSVersion.Version.Major -lt 10) {
    $targets += "HKCR:\CLSID\{D86ADE52-C4D9-4B98-AA0D-9B0C7F1EBBC8}"
    $targets += "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{9709436B-5A41-4946-8BE7-2AA433CAF108}"
    $targets += "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{FE2F6A2C-196E-4210-9C04-2B1BC21F07EF}"
}

# Add the VMware, Inc regkey
if (Test-Path "HKLM:\SOFTWARE\VMware, Inc.") {
    $targets += "HKLM:\SOFTWARE\VMware, Inc."
}
if (Test-Path "HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.") {
    $targets += "HKLM:\SOFTWARE\WOW6432Node\VMware, Inc."
}

# Add the VMware User Process run key value
$runKeyPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
if (Test-Path $runKeyPath) {
    $runKey = Get-ItemProperty -Path $runKeyPath -ErrorAction SilentlyContinue
    if ($runKey."VMware User Process") {
        # Store the registry path with value name for later deletion
        $targets += "$runKeyPath|VMware User Process"
    }
}

# Add the VMware Tools directory
if (Test-Path $VMware_Tools_Directory) {
    $targets += $VMware_Tools_Directory
}

# Thanks to @Gadgetgeek2000 for pointing out that the script leaves some 500mb of extra artifacts on disk.
# This blob removes those.
if (Test-Path $VMware_Common_Directory) {
    $targets += $VMware_Common_Directory
}

if (Test-Path $VMware_Startmenu_Entry) {
    $targets += $VMware_Startmenu_Entry
}

if (Test-Path $VMware_ProgramData_Directory) {
    $targets += $VMware_ProgramData_Directory
}

# Create a list of services to stop and remove
$services = Get-Service -DisplayName "VMware*" -ErrorAction SilentlyContinue
$services += Get-Service -DisplayName "GISvc" -ErrorAction SilentlyContinue

# Create list of VMware devices to remove
$vmwareDevices = Get-PnpDevice | Where-Object { $_.FriendlyName -like "*VMware*" }

# Warn the user about what is about to happen
if (!$targets -and !$services) {
    Write-Host "No cleanup targets found. Nothing to do!" -ForegroundColor Green
}
else {
    Write-Host "`nThe following registry keys, filesystem folders, services and devices will be deleted:" -ForegroundColor Yellow
    if ($targets) {
        Write-Host "`nTargets:" -ForegroundColor Yellow
        $targets | ForEach-Object { Write-Host "  - $_" }
    }
    if ($services) {
        Write-Host "`nServices:" -ForegroundColor Yellow
        $services | ForEach-Object { Write-Host "  - $($_.Name) ($($_.DisplayName))" }
    }
    if ($vmwareDevices) {
        Write-Host "`nDevices:" -ForegroundColor Yellow
        $vmwareDevices | ForEach-Object { Write-Host "  - $($_.FriendlyName) [$($_.InstanceId)]" }
    }

    # Check if Force parameter is used or get user confirmation
    if ($Force) {
        $cleanup_confirmed = "y"
        Write-Host "`nForce parameter specified - proceeding with cleanup without confirmation..." -ForegroundColor Yellow
    }
    else {
        $cleanup_confirmed = Read-Host "`nContinue with cleanup? (y/n)"
    }

    $global:ErrorActionPreference = 'SilentlyContinue'
    if ($cleanup_confirmed -eq "y") {
        # if vmStatsProvider.dll exists, unregister it first
        $vmStatsProvider = "c:\Program Files\VMware\VMware Tools\vmStatsProvider\win64\vmStatsProvider.dll"
        if (Test-Path $vmStatsProvider) {
            Write-Host "Unregistering vmStatsProvider.dll..." -ForegroundColor Yellow
            Regsvr32 /s /u $vmStatsProvider
        }

        # Stop all running VMware Services
        Write-Host "Stopping VMware services..." -ForegroundColor Yellow
        $services | Stop-Service -Confirm:$false -ErrorAction SilentlyContinue

        # Cover for Remove-Service not existing in PowerShell versions < 6.0
        Write-Host "Removing VMware services..." -ForegroundColor Yellow
        if (Get-Command Remove-Service -ErrorAction SilentlyContinue) {
            $services | Remove-Service -Confirm:$false -ErrorAction SilentlyContinue
        }
        else {
            foreach ($s in $services) {
                sc.exe DELETE $($s.Name)
            }
        }

        # Stop dependent services to unlock files
        Write-Host "Stopping dependent services temporarily..." -ForegroundColor Yellow
        $dep = Get-Service -Name "EventLog" -DependentServices | Select-Object -Property Name
        Stop-Service -Name "EventLog" -Force -ErrorAction SilentlyContinue
        Stop-Service -Name "wmiApSrv" -Force -ErrorAction SilentlyContinue
        $dep += Get-Service -Name "winmgmt" -DependentServices | Select-Object -Property Name
        Stop-Service -Name "winmgmt" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5

        # Remove all the files that are listed in $targets
        Write-Host "Removing registry keys, registry values, and filesystem folders..." -ForegroundColor Yellow
        foreach ($item in $targets) {
            # Check if this is a registry value (denoted by pipe separator)
            if ($item -match '^(.+)\|(.+)$') {
                $regPath = $Matches[1]
                $valueName = $Matches[2]
                if (Test-Path $regPath) {
                    Write-Verbose "Removing registry value: $valueName from $regPath"
                    Remove-ItemProperty -Path $regPath -Name $valueName -Force -ErrorAction SilentlyContinue
                }
            }
            elseif (Test-Path $item) {
                Write-Verbose "Removing: $item"
                Get-Childitem -Path $item -Recurse | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
                Remove-Item -Path $item -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        # Restart dependent services
        Write-Host "Restarting dependent services..." -ForegroundColor Yellow
        Start-Service -Name "EventLog" -ErrorAction SilentlyContinue
        Start-Service -Name "wmiApSrv" -ErrorAction SilentlyContinue
        Start-Service -Name "winmgmt" -ErrorAction SilentlyContinue
        foreach ($service in $dep) {
            Start-Service $service.Name -ErrorAction SilentlyContinue
        }

        # Remove VMware devices
        if ($vmwareDevices.Count -gt 0) {
            Write-Host "Removing VMware devices..." -ForegroundColor Yellow
            foreach ($device in $vmwareDevices) {
                Write-Verbose "Removing device: $($device.FriendlyName) [$($device.InstanceId)]"
                pnputil /remove-device $device.InstanceId 2>&1 | Out-Null
            }
        }
        else {
            Write-Host "No VMware devices found." -ForegroundColor Green
        }

        # Remove VMware driver packages from the driver store
        Write-Host "Removing VMware driver packages..." -ForegroundColor Yellow
        $pnpOutput = pnputil /enum-drivers
        $vmwareDrivers = @()

        for ($i = 0; $i -lt $pnpOutput.Count; $i++) {
            if ($pnpOutput[$i] -match "Published Name\s*:\s*(oem\d+\.inf)") {
                $oemInf = $Matches[1]
                # Check the next few lines for VMware in the original or provider name
                $driverBlock = $pnpOutput[$i..($i+5)] -join " "
                if ($driverBlock -match "VMware") {
                    $vmwareDrivers += $oemInf
                }
            }
        }

        if ($vmwareDrivers.Count -gt 0) {
            Write-Host "Found $($vmwareDrivers.Count) VMware driver package(s) in driver store" -ForegroundColor Yellow
            foreach ($driver in $vmwareDrivers) {
                Write-Verbose "Deleting driver package: $driver"
                pnputil /delete-driver $driver /uninstall /force 2>&1 | Out-Null
            }
        }
        else {
            Write-Host "No VMware driver packages found in driver store." -ForegroundColor Green
        }

        Start-Sleep -Seconds 5

        $stepDuration = (Get-Date) - $stepStartTime
        Write-Host "Step completed in $($stepDuration.TotalSeconds.ToString('F2')) seconds" -ForegroundColor Gray

        Write-Host "`n=== Cleanup Complete ===" -ForegroundColor Green
        Write-Host "Please reboot the system to complete VMware Tools removal." -ForegroundColor Yellow
    }
    else {
        Write-Host "Cleanup cancelled by user." -ForegroundColor Red
        $stepDuration = (Get-Date) - $stepStartTime
        Write-Host "Step completed in $($stepDuration.TotalSeconds.ToString('F2')) seconds" -ForegroundColor Gray
    }
}
#endregion

#region Finalize
$scriptDuration = (Get-Date) - $scriptStartTime
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "Total script execution time: $($scriptDuration.TotalSeconds.ToString('F2')) seconds ($($scriptDuration.ToString('mm\:ss')))" -ForegroundColor Cyan
Write-Host "Script completed at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "Transcript saved to: $transcriptPath" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

Stop-Transcript

# Handle reboot if requested
if ($Reboot) {
    if ($Force) {
        Write-Host "`nRebooting system now..." -ForegroundColor Yellow
        Restart-Computer -Force
    }
    else {
        $rebootConfirmed = Read-Host "`nReboot now? (y/n)"
        if ($rebootConfirmed -eq "y") {
            Write-Host "Rebooting system now..." -ForegroundColor Yellow
            Restart-Computer -Force
        }
        else {
            Write-Host "Reboot cancelled. Please reboot manually to complete removal." -ForegroundColor Yellow
        }
    }
}
#endregion