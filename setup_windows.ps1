param(
    [switch]$SkipPrerequisites,
    [switch]$NoDriver,
    [switch]$NoStartApp,
    [switch]$EnsureDriverOnly,
    [switch]$FromLauncher
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSCommandPath
$WdkConfigUrl = "https://raw.githubusercontent.com/microsoft/Windows-driver-samples/main/_wdk_utils/winget/configs/wdk-vscommunity.dsc.yaml"
$CacheDir = Join-Path $Root "driver\.cache"
$DriverCacheFile = Join-Path $CacheDir "splitter-driver-installed.json"
$SplitterHardwareId = "ROOT\SplitterAudioCable"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Restart-AsAdmin {
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($SkipPrerequisites) { $args += "-SkipPrerequisites" }
    if ($NoDriver) { $args += "-NoDriver" }
    if ($NoStartApp) { $args += "-NoStartApp" }
    if ($EnsureDriverOnly) { $args += "-EnsureDriverOnly" }
    if ($FromLauncher) { $args += "-FromLauncher" }

    Start-Process powershell.exe -Verb RunAs -ArgumentList $args
}

function Find-MSBuild {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $found = & $vswhere -latest -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
        if ($found -and (Test-Path $found)) {
            return $found
        }
    }

    $candidates = @(
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2019\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $pathMsbuild = Get-Command msbuild.exe -ErrorAction SilentlyContinue
    if ($pathMsbuild) {
        return $pathMsbuild.Source
    }

    return $null
}

function Find-WdkTool([string]$Name) {
    $fromPath = Get-Command $Name -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10",
        "$env:ProgramFiles\Windows Kits\10"
    )

    foreach ($root in $roots) {
        if (Test-Path $root) {
            $match = Get-ChildItem -Path $root -Recurse -Filter $Name -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "\\x64\\" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
            if ($match) {
                return $match.FullName
            }
        }
    }

    return $null
}

function Invoke-Logged([scriptblock]$Command, [string]$FailureMessage) {
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage Exit code: $LASTEXITCODE"
    }
}

function Write-DriverCache([string]$Status) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    @{
        status = $Status
        hardwareId = $SplitterHardwareId
        checkedAt = (Get-Date).ToString("s")
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $DriverCacheFile
}

function Test-SplitterDriverInstalled {
    $getPnpDevice = Get-Command Get-PnpDevice -ErrorAction SilentlyContinue
    if ($getPnpDevice) {
        $devices = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {
            $_.InstanceId -like "ROOT\SPLITTERAUDIOCABLE*" -or
            $_.InstanceId -like "ROOT\SplitterAudioCable*" -or
            $_.FriendlyName -like "*Splitter Output*" -or
            $_.FriendlyName -like "*Splitter Audio Cable*"
        }

        if ($devices) {
            Write-DriverCache "installed"
            return $true
        }

        return $false
    }

    if (Test-Path $DriverCacheFile) {
        try {
            $cache = Get-Content -Raw $DriverCacheFile | ConvertFrom-Json
            return $cache.status -eq "installed"
        } catch {
            return $false
        }
    }

    return $false
}

function Ensure-Git {
    if (Get-Command git.exe -ErrorAction SilentlyContinue) {
        return
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Git is missing and WinGet is not available to install it. Install Git for Windows, then rerun setup_windows.bat."
    }

    Write-Host "Git was not found. Installing Git for Windows with WinGet..."
    Invoke-Logged { winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements } "Git installation failed."

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw "Git installation finished, but git.exe is not available in this session. Close this window and rerun setup_windows.bat."
    }
}

function Ensure-WdkToolchain {
    if ((Find-MSBuild) -and (Find-WdkTool "signtool.exe") -and (Find-WdkTool "devcon.exe")) {
        Write-Host "Driver build tools found."
        return
    }

    if ($SkipPrerequisites) {
        throw "Driver build tools are missing. Rerun setup without -SkipPrerequisites, or install Visual Studio + Windows SDK + WDK."
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "WinGet is required to install the WDK toolchain automatically."
    }

    Write-Host ""
    Write-Host "Driver build tools are missing."
    Write-Host "Installing Microsoft's WDK development configuration. This is large and can take a while."
    Write-Host "Source: $WdkConfigUrl"
    Write-Host ""

    Invoke-Logged { winget configure -f $WdkConfigUrl } "WDK toolchain installation failed."

    if (-not ((Find-MSBuild) -and (Find-WdkTool "signtool.exe") -and (Find-WdkTool "devcon.exe"))) {
        throw "WDK setup completed, but required tools still were not found. Reboot or open a fresh PowerShell, then rerun setup_windows.bat."
    }
}

function Test-TestSigning {
    $output = & bcdedit /enum "{current}" 2>$null | Out-String
    return $output -match "(?im)^\s*testsigning\s+Yes\s*$"
}

function Ensure-TestSigning {
    if (Test-TestSigning) {
        Write-Host "Windows test-signing mode is already enabled."
        return $false
    }

    Write-Host "Enabling Windows test-signing mode for the locally built driver..."
    & bcdedit /set testsigning on
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enable test-signing mode. If Secure Boot is enabled, Windows may block test-signed drivers until Secure Boot is disabled."
    }

    Write-Host ""
    Write-Host "Test-signing mode was enabled. Reboot is required before the driver can be installed."
    return $true
}

function Install-SplitterDriver {
    Push-Location $Root
    try {
        & "$Root\driver\Prepare-SplitterDriver.ps1"
        & "$Root\driver\Build-SplitterDriver.ps1"

        $rebootNeeded = Ensure-TestSigning
        if ($rebootNeeded) {
            Write-Host ""
            $answer = Read-Host "Reboot now? Type Y to reboot, or press Enter to reboot later"
            if ($answer -match "^(y|yes)$") {
                Restart-Computer
            }
            throw "Reboot required. After reboot, run setup_windows.bat again to finish installing Splitter Output."
        }

        & "$Root\driver\Install-SplitterDriver.ps1"
        Write-DriverCache "installed"
    } finally {
        Pop-Location
    }
}

function Start-App {
    Start-Process -FilePath "$Root\run_windows.bat" -ArgumentList "--skip-driver-setup" -WorkingDirectory $Root
}

try {
    if ($IsLinux -or $IsMacOS) {
        throw "Run this setup from native Windows, not WSL."
    }

    if ($EnsureDriverOnly -and (Test-SplitterDriverInstalled)) {
        Write-Host "Splitter Output is already installed."
        exit 0
    }

    if (-not (Test-Admin)) {
        Write-Host "Requesting administrator permission..."
        Restart-AsAdmin
        exit 100
    }

    Write-Host "=== Splitter Audio Setup ==="
    Write-Host ""

    if (-not $NoDriver) {
        if (Test-SplitterDriverInstalled) {
            Write-Host "Splitter Output is already installed."
        } else {
            Ensure-Git
            Ensure-WdkToolchain
            Install-SplitterDriver
        }
    }

    if ($EnsureDriverOnly -and -not $FromLauncher) {
        Write-Host ""
        Write-Host "Driver setup complete."
        exit 0
    }

    if ($FromLauncher -and $EnsureDriverOnly) {
        Start-App
        exit 0
    }

    if (-not $NoStartApp) {
        Start-App
    }

    Write-Host ""
    Write-Host "Setup complete. Look for 'Splitter Output' in Windows Sound output devices."
    if (-not $FromLauncher) {
        Read-Host "Press Enter to close this setup window"
    }
} catch {
    Write-Host ""
    Write-Host "Setup failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to close this setup window"
    exit 1
}
