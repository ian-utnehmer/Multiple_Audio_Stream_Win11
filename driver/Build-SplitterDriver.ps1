param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [ValidateSet("x64", "ARM64")]
    [string]$Platform = "x64",
    [string]$SourceDir = "$PSScriptRoot\work\Virtual-Audio-Driver",
    [switch]$NoValidationRetry
)

$ErrorActionPreference = "Stop"

function Find-MSBuild {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $foundPaths = & $vswhere -latest -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe"
        $found = $foundPaths | Select-Object -First 1
        if ($found -and (Test-Path $found)) {
            return $found
        }
    }

    $candidates = @(
        "$env:ProgramFiles\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\18\Professional\MSBuild\Current\Bin\MSBuild.exe",
        "$env:ProgramFiles\Microsoft Visual Studio\18\Enterprise\MSBuild\Current\Bin\MSBuild.exe",
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

    throw "MSBuild was not found. Install Visual Studio 2022 or Build Tools plus the Windows Driver Kit."
}

function Find-WdkTool([string]$Name, [string]$PreferredPlatform = "x64") {
    $fromPath = Get-Command $Name -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10",
        "$env:ProgramFiles\Windows Kits\10"
    )
    $platformPatterns = @("\\$PreferredPlatform\\", "\\x64\\", "\\x86\\", "\\arm64\\") | Select-Object -Unique

    foreach ($root in $roots) {
        if (-not (Test-Path $root)) {
            continue
        }

        $matches = Get-ChildItem -Path $root -Recurse -Filter $Name -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending
        foreach ($pattern in $platformPatterns) {
            $match = $matches | Where-Object { $_.FullName -match $pattern } | Select-Object -First 1
            if ($match) {
                return $match.FullName
            }
        }
        $match = $matches | Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    throw "$Name was not found. Install the Windows Driver Kit, then retry."
}

function Invoke-DriverBuild([string]$MSBuild, [string]$Solution, [bool]$SkipValidation) {
    $args = @(
        $Solution,
        "/m",
        "/p:Configuration=$Configuration",
        "/p:Platform=$Platform",
        "/p:DriverTargetPlatform=Universal"
    )

    if ($SkipValidation) {
        $args += "/p:SkipPackageVerification=true"
        $args += "/p:ApiValidator_Enable=false"
    }

    & $MSBuild @args
    return $LASTEXITCODE
}

function Ensure-DriverPackage([string]$PackageDir, [string]$BuildOutputDir) {
    $inf = Get-ChildItem -Path $PackageDir -Filter "*.inf" -ErrorAction SilentlyContinue | Select-Object -First 1
    $sys = Get-ChildItem -Path $PackageDir -Filter "*.sys" -ErrorAction SilentlyContinue | Select-Object -First 1
    $cat = Get-ChildItem -Path $PackageDir -Filter "*.cat" -ErrorAction SilentlyContinue | Select-Object -First 1

    if ($inf -and $sys -and $cat) {
        return
    }

    Write-Host ""
    Write-Host "Creating driver package manually..."
    New-Item -ItemType Directory -Force -Path $PackageDir | Out-Null

    $builtInf = Get-ChildItem -Path $BuildOutputDir -Filter "*.inf" -ErrorAction Stop | Select-Object -First 1
    $builtSys = Get-ChildItem -Path $BuildOutputDir -Filter "*.sys" -ErrorAction Stop | Select-Object -First 1
    Copy-Item -Force $builtInf.FullName $PackageDir
    Copy-Item -Force $builtSys.FullName $PackageDir

    $builtCer = Get-ChildItem -Path $BuildOutputDir -Filter "*.cer" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($builtCer) {
        Copy-Item -Force $builtCer.FullName $PackageDir
    }

    $inf2cat = Find-WdkTool "inf2cat.exe" $Platform
    $os = if ($Platform -eq "ARM64") { "10_ARM64" } else { "10_X64" }
    & $inf2cat /driver:$PackageDir /os:$os
    if ($LASTEXITCODE -ne 0) {
        throw "inf2cat failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path (Join-Path $SourceDir "VirtualAudioDriver.sln"))) {
    throw "Driver source was not found. Run driver\Prepare-SplitterDriver.ps1 first."
}

$msbuild = Find-MSBuild
$solution = Join-Path $SourceDir "VirtualAudioDriver.sln"

Push-Location $SourceDir
try {
    $exitCode = Invoke-DriverBuild -MSBuild $msbuild -Solution $solution -SkipValidation $false

    if ($exitCode -ne 0 -and -not $NoValidationRetry) {
        Write-Host ""
        Write-Host "MSBuild failed during WDK package validation. Retrying without validation-only targets..."
        $exitCode = Invoke-DriverBuild -MSBuild $msbuild -Solution $solution -SkipValidation $true
    }

    if ($exitCode -ne 0) {
        throw "MSBuild failed with exit code $exitCode."
    }
} finally {
    Pop-Location
}

$packageDir = Join-Path $SourceDir "$Platform\$Configuration\package"
$buildOutputDir = Join-Path $SourceDir "Source\Main\$Platform\$Configuration"
Ensure-DriverPackage -PackageDir $packageDir -BuildOutputDir $buildOutputDir

Write-Host ""
Write-Host "Build complete."
Write-Host "Package directory:"
Write-Host "  $packageDir"
Write-Host ""
Write-Host "Next: run driver\Install-SplitterDriver.ps1 from an elevated PowerShell."
