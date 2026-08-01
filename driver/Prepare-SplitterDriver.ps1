param(
    [string]$RepoUrl = "https://github.com/VirtualDrivers/Virtual-Audio-Driver.git",
    [string]$Ref = "25.7.14",
    [string]$WorkDir = "$PSScriptRoot\work",
    [string]$DriverName = "Splitter Audio Cable",
    [string]$PlaybackName = "Splitter Output",
    [string]$CaptureName = "Splitter Input",
    [string]$ProviderName = "SoundProject",
    [string]$HardwareId = "ROOT\SplitterAudioCable"
)

$ErrorActionPreference = "Stop"

function Assert-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found in PATH."
    }
}

function Replace-Literal([string]$Path, [hashtable]$Replacements, [string]$Encoding = "Unicode") {
    $text = [System.IO.File]::ReadAllText($Path)
    foreach ($key in $Replacements.Keys) {
        $text = $text.Replace($key, [string]$Replacements[$key])
    }
    [System.IO.File]::WriteAllText($Path, $text, [System.Text.Encoding]::$Encoding)
}

Assert-Command git

$sourceDir = Join-Path $WorkDir "Virtual-Audio-Driver"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

if (-not (Test-Path (Join-Path $sourceDir ".git"))) {
    git clone --depth 1 --branch $Ref $RepoUrl $sourceDir
} else {
    Write-Host "Using existing driver source at $sourceDir"
    Write-Host "Delete that folder if you want a fresh checkout."
}

$inx = Join-Path $sourceDir "Source\Main\VirtualAudioDriver.inx"
$rc = Join-Path $sourceDir "Source\Main\VirtualAudioDriver.rc"
if (-not (Test-Path $inx)) {
    throw "Could not find expected driver installer template: $inx"
}
if (-not (Test-Path $rc)) {
    throw "Could not find expected driver resource file: $rc"
}

Replace-Literal -Path $inx -Replacements @{
    'ROOT\VirtualAudioDriver' = $HardwareId
    'ProviderName = "MikeTheTech"' = "ProviderName = `"$ProviderName`""
    'MfgName      = "MikeTheTech"' = "MfgName      = `"$ProviderName`""
    'MsCopyRight  = "MikeTheTech"' = "MsCopyRight  = `"$ProviderName`""
    'VIRTUALAUDIODRIVER_SA.DeviceDesc="Virtual Audio Driver by MTT"' = "VIRTUALAUDIODRIVER_SA.DeviceDesc=`"$DriverName`""
    'VirtualAudioDriver.SvcDesc="Virtual Audio Driver by MTT"' = "VirtualAudioDriver.SvcDesc=`"$DriverName`""
    'VIRTUALAUDIODRIVER.WaveSpeaker.szPname="Virtual Audio Driver by MTT"' = "VIRTUALAUDIODRIVER.WaveSpeaker.szPname=`"$PlaybackName`""
    'VIRTUALAUDIODRIVER.TopologySpeaker.szPname="Virtual Audio Driver by MTT"' = "VIRTUALAUDIODRIVER.TopologySpeaker.szPname=`"$PlaybackName`""
    'VIRTUALAUDIODRIVER.WaveMicArray1.szPname="Virtual Mic Driver by MTT"' = "VIRTUALAUDIODRIVER.WaveMicArray1.szPname=`"$CaptureName`""
    'VIRTUALAUDIODRIVER.TopologyMicArray1.szPname="Virtual Mic Driver by MTT"' = "VIRTUALAUDIODRIVER.TopologyMicArray1.szPname=`"$CaptureName`""
    'MicArray1CustomName= "Virtual Mic Driver by MTT"' = "MicArray1CustomName= `"$CaptureName`""
}

Replace-Literal -Path $rc -Encoding UTF8 -Replacements @{
    'Microsoft Virtual Simple Audio Sample Driver' = $DriverName
}

$metadata = @{
    repo = $RepoUrl
    ref = $Ref
    source = $sourceDir
    hardwareId = $HardwareId
    playbackName = $PlaybackName
    captureName = $CaptureName
    preparedAt = (Get-Date).ToString("s")
}
$metadata | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $WorkDir "splitter-driver.json")

Write-Host ""
Write-Host "Prepared Splitter driver source:"
Write-Host "  Source:      $sourceDir"
Write-Host "  Playback:    $PlaybackName"
Write-Host "  Capture:     $CaptureName"
Write-Host "  Hardware ID: $HardwareId"
Write-Host ""
Write-Host "Next: run driver\Build-SplitterDriver.ps1 from a Windows Developer PowerShell with Visual Studio + WDK installed."
