param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [ValidateSet("x64", "ARM64")]
    [string]$Platform = "x64",
    [string]$SourceDir = "$PSScriptRoot\work\Virtual-Audio-Driver"
)

$ErrorActionPreference = "Stop"

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

    throw "MSBuild was not found. Install Visual Studio 2022 or Build Tools plus the Windows Driver Kit."
}

if (-not (Test-Path (Join-Path $SourceDir "VirtualAudioDriver.sln"))) {
    throw "Driver source was not found. Run driver\Prepare-SplitterDriver.ps1 first."
}

$msbuild = Find-MSBuild
$solution = Join-Path $SourceDir "VirtualAudioDriver.sln"

Push-Location $SourceDir
try {
    & $msbuild $solution `
        /m `
        /p:Configuration=$Configuration `
        /p:Platform=$Platform `
        /p:DriverTargetPlatform=Universal

    if ($LASTEXITCODE -ne 0) {
        throw "MSBuild failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

$packageDir = Join-Path $SourceDir "$Platform\$Configuration\package"
Write-Host ""
Write-Host "Build complete."
Write-Host "Package directory:"
Write-Host "  $packageDir"
Write-Host ""
Write-Host "Next: run driver\Install-SplitterDriver.ps1 from an elevated PowerShell."
