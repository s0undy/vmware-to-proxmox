# Purpose of this script
# * make Windows load the VirtIO SCSI pass-through driver on boot without reboot or external programs
#
# Prerequisites
# * VirtIO SCSI driver (vioscsi) must be installed OR path to the INF-File must be given as argument to the script
# * Windows 7/8 or Windows Server 2008 or newer
#
# Remarks
# * On Windows before Windows Server 2022 and Windows 10 2004 the software device created by the script will not be removed automatically, but this can be done via device manager. It can also be left there, no harm in that.
# * You should have a backup of your system.
# * This script was tested only 
# ** on Windows Server 2025 Standard Edition (24H2)
# ** on Windows Server 2022 Datacenter Edition (21H2)
# ** with virtio drivers version .266, .271, .285
# * nearly all code in here was written by AI
#
# How to use
# either
# * install the vioscsi.inf before OR
# * tell the script to do it by -DriverPath parameter (path to folder with OS subfolders: 2k12R2, 2k16, 2k19, 2k22, 2k25) OR
# * use virtio-win-guest-tools.exe /S to install it
# depending on the set security policy it might be required to run the script via
# PowerShell /ExecutionPolicy Bypass /File <path-to-this-script-file> [-DriverPath <path-to-vioscsi-driver-folder>]
#
# How does the script work?
# * install the vioscsi driver, if requested
# * create a software based device via Win32-API
# * add some registry settings
# * use pnputil to install the driver for this device
# * use pnputil to remove the device
# The driver-to-device assignment marks the driver for being loaded on boot.
# Now the Windows is ready for migration to a VirtIO based SCSI disk.

# ==== PARAMETER ====
param(
    [string]$DriverPath = "",  # Optional: Path to folder containing OS subfolders (2k8, 2k8R2, 2k12, 2k12R2, 2k16, 2k19, 2k22, 2k25, w7, w8, w8.1, w10, w11)
    [string]$LogPath = "$env:windir\Temp\vioscsi-boot-loader.log"  # Log file path (CMTrace-compatible)
)

# ==== LOGGING ====
function Write-Log {
    param(
        [string]$Message,
        [ValidateSet("Info","Warn","Error")]
        [string]$Severity = "Info"
    )
    $typeMap = @{ "Info" = 1; "Warn" = 2; "Error" = 3 }
    $colorMap = @{ "Info" = "White"; "Warn" = "Yellow"; "Error" = "Red" }
    $time = Get-Date -Format "HH:mm:ss.fff"
    $date = Get-Date -Format "MM-dd-yyyy"
    $entry = "<![LOG[$Message]LOG]!><time=`"$time`" date=`"$date`" component=`"vioscsi-loader`" context=`"`" type=`"$($typeMap[$Severity])`" thread=`"`" file=`"`">"
    $entry | Out-File -Append -Encoding utf8 -FilePath $LogPath
    Write-Host $Message -ForegroundColor $colorMap[$Severity]
}

# ==== OS DETECTION & INF PATH RESOLUTION ====
$InfPath = ""
if ($DriverPath) {
    if (-not (Test-Path $DriverPath)) {
        throw "Driver path does not exist: $DriverPath"
    }

    $osVersion = [System.Environment]::OSVersion.Version
    $buildNumber = [int](Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber
    $productType = (Get-CimInstance Win32_OperatingSystem).ProductType
    # ProductType: 1 = Workstation, 2 = Domain Controller, 3 = Server

    $osFolder = $null
    if ($productType -eq 1) {
        # Client OS
        if ($osVersion.Major -eq 10 -and $buildNumber -ge 22000) {
            $osFolder = "w11"
        } elseif ($osVersion.Major -eq 10) {
            $osFolder = "w10"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 3) {
            $osFolder = "w8.1"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 2) {
            $osFolder = "w8"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 1) {
            $osFolder = "w7"
        }
    } else {
        # Server OS
        if ($osVersion.Major -eq 10 -and $buildNumber -ge 26100) {
            $osFolder = "2k25"
        } elseif ($osVersion.Major -eq 10 -and $buildNumber -ge 20348) {
            $osFolder = "2k22"
        } elseif ($osVersion.Major -eq 10 -and $buildNumber -ge 17763) {
            $osFolder = "2k19"
        } elseif ($osVersion.Major -eq 10 -and $buildNumber -ge 14393) {
            $osFolder = "2k16"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 3) {
            $osFolder = "2k12R2"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 2) {
            $osFolder = "2k12"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 1) {
            $osFolder = "2k8R2"
        } elseif ($osVersion.Major -eq 6 -and $osVersion.Minor -eq 0) {
            $osFolder = "2k8"
        }
    }

    if (-not $osFolder) {
        throw "Could not determine OS folder for Windows version $($osVersion.Major).$($osVersion.Minor) Build $buildNumber"
    }

    $InfPath = Join-Path (Join-Path (Join-Path $DriverPath $osFolder) "amd64") "vioscsi.inf"
    if (-not (Test-Path $InfPath)) {
        throw "Driver INF not found at expected path: $InfPath"
    }

    Write-Log "Detected OS folder: $osFolder"
    Write-Log "Resolved INF path: $InfPath"
}

$source = @"
using System;
using System.Runtime.InteropServices;

public class DeviceInstaller
{
    private const uint DIF_REGISTERDEVICE = 0x00000019;
    private const uint DIF_SELECTBESTCOMPATDRV = 0x00000017;
    private const uint DIF_INSTALLDEVICE = 0x00000002;
    private const uint DICD_GENERATE_ID = 0x00000001;
    private const uint SPDIT_COMPATDRIVER = 0x00000001;
    
    [StructLayout(LayoutKind.Sequential)]
    private struct SP_DEVINFO_DATA
    {
        public uint cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }
    
    [DllImport("setupapi.dll", SetLastError = true, CharSet = CharSet.Auto)]
    private static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid ClassGuid, IntPtr hwndParent);
    
    [DllImport("setupapi.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern bool SetupDiCreateDeviceInfo(
        IntPtr DeviceInfoSet,
        string DeviceName,
        ref Guid ClassGuid,
        string DeviceDescription,
        IntPtr hwndParent,
        uint CreationFlags,
        ref SP_DEVINFO_DATA DeviceInfoData);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiSetDeviceRegistryProperty(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint Property,
        byte[] PropertyBuffer,
        uint PropertyBufferSize);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiRegisterDeviceInfo(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint Flags,
        IntPtr CompareProc,
        IntPtr CompareContext,
        IntPtr DupDeviceInfo);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiCallClassInstaller(
        uint InstallFunction,
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiBuildDriverInfoList(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData,
        uint DriverType);
    
    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiInstallDevice(
        IntPtr DeviceInfoSet,
        ref SP_DEVINFO_DATA DeviceInfoData);
    
    public static void CreateSoftwareDeviceWithHardwareId(string deviceId, string classGuidStr, string hardwareId)
    {
        Guid classGuid = new Guid(classGuidStr);
        IntPtr devInfoSet = SetupDiCreateDeviceInfoList(ref classGuid, IntPtr.Zero);
        
        if (devInfoSet == IntPtr.Zero || devInfoSet.ToInt64() == -1)
        {
            int error = Marshal.GetLastWin32Error();
            throw new Exception("SetupDiCreateDeviceInfoList failed with error " + error);
        }
        
        try
        {
            SP_DEVINFO_DATA devInfoData = new SP_DEVINFO_DATA();
            devInfoData.cbSize = (uint)Marshal.SizeOf(typeof(SP_DEVINFO_DATA));
            devInfoData.ClassGuid = classGuid;
            
            if (!SetupDiCreateDeviceInfo(devInfoSet, deviceId, ref classGuid, "VirtIO SCSI Controller", IntPtr.Zero, DICD_GENERATE_ID, ref devInfoData))
            {
                int error = Marshal.GetLastWin32Error();
                throw new Exception("SetupDiCreateDeviceInfo failed with error " + error);
            }
            
            byte[] hwid = System.Text.Encoding.ASCII.GetBytes(hardwareId + "\0\0");
            if (!SetupDiSetDeviceRegistryProperty(devInfoSet, ref devInfoData, 1, hwid, (uint)hwid.Length))
            {
                int error = Marshal.GetLastWin32Error();
                throw new Exception("SetupDiSetDeviceRegistryProperty failed with error " + error);
            }
            
            if (!SetupDiRegisterDeviceInfo(devInfoSet, ref devInfoData, 0, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero))
            {
                int error = Marshal.GetLastWin32Error();
                throw new Exception("SetupDiRegisterDeviceInfo failed with error " + error);
            }
            
            if (!SetupDiCallClassInstaller(DIF_REGISTERDEVICE, devInfoSet, ref devInfoData))
            {
                int error = Marshal.GetLastWin32Error();
                throw new Exception("SetupDiCallClassInstaller failed with error " + error);
            }
            
            // Driver installation
            Console.WriteLine("Building driver info list...");
            if (SetupDiBuildDriverInfoList(devInfoSet, ref devInfoData, SPDIT_COMPATDRIVER))
            {
                Console.WriteLine("Driver info list built successfully");
                
                // Select compatible driver
                if (SetupDiCallClassInstaller(DIF_SELECTBESTCOMPATDRV, devInfoSet, ref devInfoData))
                {
                    Console.WriteLine("Best compatible driver selected");
                    
                    // Install driver
                    if (SetupDiInstallDevice(devInfoSet, ref devInfoData))
                    {
                        Console.WriteLine("Driver installed successfully");
                    }
                    else
                    {
                        int error = Marshal.GetLastWin32Error();
                        Console.WriteLine("SetupDiInstallDevice failed with error " + error);
                    }
                    
                    // Call device installation class installer
                    if (SetupDiCallClassInstaller(DIF_INSTALLDEVICE, devInfoSet, ref devInfoData))
                    {
                        Console.WriteLine("Device installation completed");
                    }
                    else
                    {
                        int error = Marshal.GetLastWin32Error();
                        Console.WriteLine("DIF_INSTALLDEVICE failed with error " + error);
                    }
                }
                else
                {
                    int error = Marshal.GetLastWin32Error();
                    Console.WriteLine("DIF_SELECTBESTCOMPATDRV failed with error " + error);
                }
            }
            else
            {
                int error = Marshal.GetLastWin32Error();
                Console.WriteLine("SetupDiBuildDriverInfoList failed with error " + error);
            }
        }
        finally
        {
            SetupDiDestroyDeviceInfoList(devInfoSet);
        }
    }
}
"@

# Checking for administrator access
if (-NOT ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    Write-Log "This script must be run with administrator privileges!" -Severity Error
    exit 1
}

# Add compiled class
Add-Type -TypeDefinition $source

$deviceId = "vioscsi"
$classGuid = "{4d36e97b-e325-11ce-bfc1-08002be10318}"
$hardwareId = "PCI\VEN_1AF4&DEV_1004&SUBSYS_00081AF4&REV_00"

# Registry path for VirtIO installation
$virtioBasePath = "HKLM:\SOFTWARE\RedHat\Virtio-Win\Components\vioscsi"

try {
    # OPTIONAL: Install INF driver first if path provided
    if ($InfPath -and (Test-Path $InfPath)) {
        Write-Log "Installing INF driver from provided path..." -Severity Warn
        Write-Log "INF Path: $InfPath"

        # Check Windows version for correct pnputil syntax
        $osVersion = [System.Environment]::OSVersion.Version
        $buildNumber = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber
        $useNewSyntax = ($osVersion.Major -gt 10) -or
                       (($osVersion.Major -eq 10) -and ([int]$buildNumber -ge 14393)) -or
                       (($osVersion.Major -eq 6) -and ($osVersion.Minor -ge 3))

        try {
            if ($useNewSyntax) {
                Write-Log "Executing: pnputil /add-driver `"$InfPath`" /install"
                & pnputil /add-driver "$InfPath" /install
            } else {
                Write-Log "Executing: pnputil -a -i `"$InfPath`""
                & pnputil -a -i "$InfPath"
            }

            if ($LASTEXITCODE -eq 0) {
                Write-Log "INF driver installed successfully!"
            } else {
                Write-Log "INF installation completed with exit code $LASTEXITCODE" -Severity Warn
            }
        } catch {
            Write-Log "INF installation failed: $($_.Exception.Message)" -Severity Error
            throw "Cannot continue without driver installation"
        }

        Write-Log "Waiting for driver registration..."
        Start-Sleep -Seconds 2
    } elseif ($InfPath) {
        throw "Provided INF path does not exist: $InfPath"
    }
    # Check if VirtIO registry entries exist
    if (-not (Test-Path $virtioBasePath)) {
        Write-Log "VirtIO guest tools registry not found, searching DriverDatabase..." -Severity Warn
        
        # Fallback: Search in DriverDatabase for vioscsi.inf
        $driverDbPath = "HKLM:\SYSTEM\DriverDatabase\DriverPackages"
        $vioscsiDrivers = Get-ChildItem $driverDbPath | Where-Object { $_.Name -like "*vioscsi.inf*" }
        
        if ($vioscsiDrivers) {
            # Take the first matching entry
            $driverEntry = $vioscsiDrivers[0]
            $oemInfName = (Get-ItemProperty -Path $driverEntry.PSPath).'(default)'
            
            if ($oemInfName) {
                Write-Log "Found vioscsi driver in DriverDatabase: $($driverEntry.Name.Split('\')[-1])"
                Write-Log "OEM INF name: $oemInfName"
                $oemInf = "$env:windir\INF\$oemInfName"
                $infFullPath = $oemInf  # Set for consistency
            } else {
                throw "Could not read OEM INF name from DriverDatabase entry"
            }
        } else {
            throw "VirtIO SCSI driver not found in DriverDatabase. Please install vioscsi.inf or virtio-win-guest-tools.exe first."
        }
    } else {
        # Original method: Read from VirtIO guest tools registry
        Write-Log "Reading VirtIO registry values..." -Severity Warn
        $virtioProps = Get-ItemProperty -Path $virtioBasePath
        $infFullPath = $virtioProps.strongname
        $oemInfName = $virtioProps.oem
        
        if (-not $infFullPath -or -not $oemInfName) {
            throw "Registry values strongname or oem not found"
        }
        
        Write-Log "INF path found: $infFullPath"
        Write-Log "OEM name found: $oemInfName"
        $oemInf = "$env:windir\INF\$oemInfName"
    }

    # Step 1: Create device
    Write-Log "Step 1: Creating device using Setup API..."

    [DeviceInstaller]::CreateSoftwareDeviceWithHardwareId($deviceId, $classGuid, $hardwareId)
    Write-Log "Device created successfully using Setup API!"

    # Step 2: Assign driver
    Write-Log "Step 2: Configuring driver assignment via registry..."
    
    $targetHardwareId = $hardwareId
    
    # Find the created device path
    $devicePath = "HKLM:\SYSTEM\CurrentControlSet\Enum\ROOT\$deviceId\0000"
    
    if (-not (Test-Path $devicePath)) {
        Write-Log "Warning: Created device not found at expected path $devicePath" -Severity Warn
        $enumPath = "HKLM:\SYSTEM\CurrentControlSet\Enum\ROOT"
        $vioscsiDevices = Get-ChildItem $enumPath | Where-Object { $_.Name -like "*vioscsi*" -or $_.Name -like "*$deviceId*" }
        if ($vioscsiDevices) {
            $devicePath = $vioscsiDevices[0].PSPath + "\0000"
            Write-Log "Found device at: $devicePath"
        }
    }
    
    if (Test-Path $devicePath) {
        Write-Log "=== BEFORE Registry modifications ==="
        $originalProps = Get-ItemProperty -Path $devicePath
        Write-Log "Original Hardware ID: $($originalProps.HardwareID)"
        Write-Log "Original Class: $($originalProps.Class)"
        Write-Log "Original Service: $($originalProps.Service)"
        Write-Log "Original ClassGUID: $($originalProps.ClassGUID)"

        Write-Log "Checking device registry configuration..."
        
        # Add only missing values, no overwriting
        if (-not $originalProps.Service) {
            Set-ItemProperty -Path $devicePath -Name "Service" -Value "vioscsi" -Type String
            Write-Log "Added Service: vioscsi"
        } else {
            Write-Log "Service already set: $($originalProps.Service)"
        }
        
        if (-not $originalProps.Class) {
            Set-ItemProperty -Path $devicePath -Name "Class" -Value "SCSIAdapter" -Type String
            Write-Log "Added Class: SCSIAdapter"
        } else {
            Write-Log "Class already set: $($originalProps.Class)"
        }
        
        if (-not $originalProps.DeviceDesc) {
            Set-ItemProperty -Path $devicePath -Name "DeviceDesc" -Value "@$oemInfName,%virtioscsi.devicedesc%;VirtIO SCSI Controller" -Type String
            Write-Log "Added DeviceDesc"
        } else {
            Write-Log "DeviceDesc already set"
        }
        
        if (-not $originalProps.Mfg) {
            Set-ItemProperty -Path $devicePath -Name "Mfg" -Value "@$oemInfName,%vendor%;Red Hat, Inc." -Type String
            Write-Log "Added Mfg"
        } else {
            Write-Log "Mfg already set"
        }
        
        if (-not $originalProps.CompatibleIDs) {
            $compatibleIds = @("PCI\VEN_1AF4&DEV_1004", "PCI\VEN_1AF4", "PCI\CC_010000", "PCI\CC_0100")
            Set-ItemProperty -Path $devicePath -Name "CompatibleIDs" -Value $compatibleIds -Type MultiString
            Write-Log "Added CompatibleIDs"
        } else {
            Write-Log "CompatibleIDs already set"
        }
        
        Write-Log "Device registry configuration checked"
    }
    
    # Step 2a: Pre-configure driver binding to force immediate loading
    Write-Log "Step 2a: Pre-configuring driver binding..."
    
    # Create driver binding in Control\Class to force immediate association
    $classPath = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e97b-e325-11ce-bfc1-08002be10318}"
    
    # Find next available driver number
    $existingNumbers = @()
    $classItems = Get-ChildItem $classPath -ErrorAction SilentlyContinue
    if ($classItems) {
        foreach ($item in $classItems) {
            if ($item.Name -match '\\(\d{4})$') {
                $existingNumbers += [int]$Matches[1]
            }
        }
    }
    
    $nextNumber = if ($existingNumbers) { ($existingNumbers | Measure-Object -Maximum).Maximum + 1 } else { 1 }
    $nextNumberStr = $nextNumber.ToString("0000")
    
    # Create Control\Class entry for immediate driver binding
    $newClassPath = "$classPath\$nextNumberStr"
    New-Item -Path $newClassPath -Force | Out-Null
    
    # Set driver class properties  
    Set-ItemProperty -Path $newClassPath -Name "InfPath" -Value $oemInfName -Type String
    Set-ItemProperty -Path $newClassPath -Name "InfSection" -Value "scsi_inst" -Type String
    Set-ItemProperty -Path $newClassPath -Name "ProviderName" -Value "Red Hat, Inc." -Type String
    Set-ItemProperty -Path $newClassPath -Name "DriverDate" -Value "10-21-2024" -Type String
    Set-ItemProperty -Path $newClassPath -Name "DriverVersion" -Value "100.100.104.26600" -Type String
    Set-ItemProperty -Path $newClassPath -Name "MatchingDeviceId" -Value $targetHardwareId -Type String
    Set-ItemProperty -Path $newClassPath -Name "DriverDesc" -Value "Red Hat VirtIO SCSI pass-through controller" -Type String
    
    Write-Log "Driver class binding created: $nextNumberStr"
    
    # Pre-configure the device to reference this driver
    if (Test-Path $devicePath) {
        Set-ItemProperty -Path $devicePath -Name "Driver" -Value "{4d36e97b-e325-11ce-bfc1-08002be10318}\$nextNumberStr" -Type String
        Set-ItemProperty -Path $devicePath -Name "Problem" -Value 0 -Type DWord
        Set-ItemProperty -Path $devicePath -Name "StatusFlags" -Value 0x18 -Type DWord
        Write-Log "Device pre-configured with driver binding"
    }
    
    # Step 3: Create Critical Device Database entries
    Write-Log "Step 3: Creating Critical Device Database entries..."
    
    # Critical Device Database paths
    $criticalDbPaths = @(
        "HKLM:\SYSTEM\CurrentControlSet\Control\CriticalDeviceDatabase\pci#ven_1af4&dev_1004",
        "HKLM:\SYSTEM\CurrentControlSet\Control\CriticalDeviceDatabase\pci#ven_1af4&dev_1004&subsys_00081af4&rev_00"  
    )
    
    $criticalDbCount = 0
    foreach ($criticalPath in $criticalDbPaths) {
        $pathExists = Test-Path $criticalPath
        if (-not $pathExists) {
            New-Item -Path $criticalPath -Force -ErrorAction SilentlyContinue | Out-Null
        }
        
        # Verify path was created
        if (Test-Path $criticalPath) {
            Set-ItemProperty -Path $criticalPath -Name "Service" -Value "vioscsi" -Type String -ErrorAction SilentlyContinue
            Set-ItemProperty -Path $criticalPath -Name "ClassGUID" -Value "{4d36e97b-e325-11ce-bfc1-08002be10318}" -Type String -ErrorAction SilentlyContinue
            $criticalDbCount++
            
            $shortPath = $criticalPath.Split('\')[-1]
            Write-Log "Critical DB entry created: $shortPath"
        }
    }
    
    Write-Log "Total Critical DB entries created: $criticalDbCount/$($criticalDbPaths.Count)" -Severity $(if ($criticalDbCount -eq $criticalDbPaths.Count) { 'Info' } else { 'Warn' })
    
    # Step 4: Trigger device enumeration
    Write-Log "Step 4: Triggering device enumeration..."
    
    # Simple Configuration Manager API for ROOT enumeration only
    Add-Type @"
using System;
using System.Runtime.InteropServices;

public class ConfigManager {
    public const uint CR_SUCCESS = 0;
    public const uint CM_REENUMERATE_SYNCHRONOUS = 0x00000001;
    
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    public static extern uint CM_Locate_DevNode(out uint pdnDevInst, string pDeviceID, uint ulFlags);
    
    [DllImport("cfgmgr32.dll")]
    public static extern uint CM_Reenumerate_DevNode(uint dnDevInst, uint ulFlags);
    
    public static bool ReenumerateRootDevices() {
        uint rootDevInst;
        uint result = CM_Locate_DevNode(out rootDevInst, "ROOT", 0);
        if (result == CR_SUCCESS) {
            result = CM_Reenumerate_DevNode(rootDevInst, CM_REENUMERATE_SYNCHRONOUS);
            return result == CR_SUCCESS;
        }
        return false;
    }
}
"@
    
    # Re-enumerate ROOT devices
    Write-Log "Re-enumerating ROOT device tree..."
    $rootSuccess = [ConfigManager]::ReenumerateRootDevices()
    if ($rootSuccess) {
        Write-Log "ROOT enumeration successful"
    } else {
        Write-Log "ROOT enumeration completed (result unknown)" -Severity Warn
    }
    
    # Wait for processing
    Write-Log "Waiting for Windows to process enumeration..."
    Start-Sleep -Seconds 3

    # Step 5: Verification
    Write-Log "=== VERIFICATION ==="

    # Check device
    if (Test-Path $devicePath) {
        $deviceProps = Get-ItemProperty -Path $devicePath
        Write-Log "Device:"
        Write-Log "  Hardware ID: $($deviceProps.HardwareID[0])"
        Write-Log "  Service: $($deviceProps.Service)"
        Write-Log "  Class: $($deviceProps.Class)"
        Write-Log "  Driver: $($deviceProps.Driver)"
    }

    Write-Log "Device creation and driver assignment complete!"

    # FINAL STEP: Install driver using pnputil
    Write-Log "Final Step: Installing driver using pnputil..."

    # Check Windows version for correct pnputil syntax
    $osVersion = [System.Environment]::OSVersion.Version
    $buildNumber = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber

    Write-Log "Detected Windows version: $($osVersion.Major).$($osVersion.Minor) Build $buildNumber"
    
    # Windows 10 1607 = Build 14393, where /add-driver /install was introduced
    $useNewSyntax = ($osVersion.Major -gt 10) -or 
                   (($osVersion.Major -eq 10) -and ([int]$buildNumber -ge 14393)) -or
                   (($osVersion.Major -eq 6) -and ($osVersion.Minor -ge 3))  # Windows 8.1+
    
    try {
        if ($useNewSyntax) {
            Write-Log "Using modern pnputil syntax: /add-driver /install"
            Write-Log "Executing: pnputil /add-driver `"$oemInf`" /install"
            & pnputil /add-driver "$oemInf" /install
        } else {
            Write-Log "Using legacy pnputil syntax: -a -i"
            Write-Log "Executing: pnputil -a -i `"$oemInf`""
            & pnputil -a -i "$oemInf"
        }

        if ($LASTEXITCODE -eq 0) {
            Write-Log "Driver successfully installed via pnputil!"
        } else {
            Write-Log "pnputil completed with exit code $LASTEXITCODE" -Severity Warn
        }
    } catch {
        Write-Log "pnputil execution failed: $($_.Exception.Message)" -Severity Error

        # Fallback: Try the other syntax if the first one failed
        try {
            if ($useNewSyntax) {
                Write-Log "Trying fallback with legacy syntax..." -Severity Warn
                & pnputil -a -i "$oemInf"
            } else {
                Write-Log "Trying fallback with modern syntax..." -Severity Warn
                & pnputil /add-driver "$oemInf" /install
            }

            if ($LASTEXITCODE -eq 0) {
                Write-Log "Driver installed with fallback syntax!"
            }
        } catch {
            Write-Log "Both pnputil syntaxes failed" -Severity Error
        }
    }
	    # Check Windows version for pnputil /remove-device support
    $buildNumber = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion").CurrentBuildNumber
    $supportsRemoveDevice = ([int]$buildNumber -ge 19041)
    
    if (-not $supportsRemoveDevice) {
        Write-Log "pnputil /remove-device not supported on this Windows version (Build $buildNumber)" -Severity Warn
        Write-Log "Required: Windows 10 2004+ (Build 19041+) or Windows Server 2022+" -Severity Warn
        Write-Log "Manual removal required:"
        Write-Log "- Device Manager: devmgmt.msc -> Uninstall VirtIO SCSI Controller"
        Write-Log "- devcon.exe: devcon remove ROOT\\VIOSCSI\\0000"
        return
    }

    Write-Log "Step 1: Finding VirtIO phantom devices..."

    # Find devices using pnputil
    Write-Log "Enumerating devices with pnputil..."
    $deviceList = & pnputil /enum-devices 2>&1
    
    # Parse output to find VirtIO devices
    $virtioDevices = @()
    $currentDevice = $null
    $currentInstanceId = $null
    
    foreach ($line in $deviceList) {
        if ($line -match "Instance ID:\s+(.+)") {
            $currentInstanceId = $Matches[1].Trim()
        }
        elseif ($line -match "Device Description:\s+(.+)") {
            $deviceDesc = $Matches[1].Trim()
            # Only target ROOT enumerated devices (phantom devices) with vioscsi
            if ($currentInstanceId -like "ROOT\*$deviceId*") {
                $virtioDevices += [PSCustomObject]@{
                    InstanceId = $currentInstanceId
                    Description = $deviceDesc
                }
                Write-Log "Found VirtIO phantom device: $deviceDesc [$currentInstanceId]"
            }
        }
    }
    
    if ($virtioDevices.Count -eq 0) {
        Write-Log "No VirtIO phantom devices found" -Severity Warn
    } else {
        Write-Log "Found $($virtioDevices.Count) VirtIO device(s) to remove"
    }

    Write-Log "Step 2: Removing devices using pnputil..."
    
    $removedCount = 0
    foreach ($device in $virtioDevices) {
        Write-Log "Removing device: $($device.Description)"
        Write-Log "Instance ID: $($device.InstanceId)"

        try {
            # Remove device using pnputil
            Write-Log "Executing: pnputil /remove-device `"$($device.InstanceId)`""
            & pnputil /remove-device "$($device.InstanceId)"

            if ($LASTEXITCODE -eq 0) {
                Write-Log "Device removed successfully"
                $removedCount++
            } else {
                Write-Log "pnputil completed with exit code $LASTEXITCODE" -Severity Warn
            }
        } catch {
            Write-Log "Failed to remove device: $($_.Exception.Message)" -Severity Error
        }
    }
    
    Write-Log "Step 3: Verification..."

    # Check if devices still exist
    $remainingDevices = Get-WmiObject -Class Win32_PnPEntity -Filter "DeviceID LIKE 'ROOT\\%vioscsi%'" -ErrorAction SilentlyContinue
    if ($remainingDevices) {
        Write-Log "Some VirtIO phantom devices still visible:" -Severity Warn
        foreach ($device in $remainingDevices) {
            Write-Log "  - $($device.Name) [$($device.Status)]" -Severity Warn
        }
    } else {
        Write-Log "No VirtIO phantom devices found in system"
    }

    Write-Log "=== REMOVAL COMPLETED ==="
    Write-Log "pnputil-based phantom device removal complete!"

    Write-Log "Summary:"
    Write-Log "- Devices removed via pnputil: $removedCount"

    if ($removedCount -eq 0 -and $virtioDevices.Count -eq 0) {
        Write-Log "No phantom devices found - system is already clean"
    }
} catch {
    Write-Log "=== ERROR ===" -Severity Error
    $errorMessage = $_.Exception.Message
    Write-Log "Error during device creation and configuration or removal: $errorMessage" -Severity Error
    exit 1
}
exit 0
