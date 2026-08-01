param(
    [string]$HardwareId = "ROOT\SplitterAudioCable"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell window."
    }
}

function Find-DevCon {
    $fromPath = Get-Command devcon.exe -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10",
        "$env:ProgramFiles\Windows Kits\10"
    )
    foreach ($root in $roots) {
        if (Test-Path $root) {
            $match = Get-ChildItem -Path $root -Recurse -Filter devcon.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "\\x64\\" } |
                Sort-Object FullName -Descending |
                Select-Object -First 1
            if ($match) {
                return $match.FullName
            }
        }
    }

    throw "devcon.exe was not found. Install the Windows Driver Kit, or remove the device from Device Manager."
}

Assert-Admin
$devcon = Find-DevCon

Write-Host "Removing $HardwareId..."
& $devcon remove $HardwareId
if ($LASTEXITCODE -ne 0) {
    throw "devcon remove failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "Removed the Splitter virtual audio device. You can also remove the driver package from Device Manager if Windows keeps a stale package copy."
