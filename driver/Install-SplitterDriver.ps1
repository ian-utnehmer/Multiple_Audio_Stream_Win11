param(
    [string]$PackageDir = "$PSScriptRoot\work\Virtual-Audio-Driver\x64\Release\package",
    [string]$HardwareId = "ROOT\SplitterAudioCable",
    [string]$CertSubject = "Splitter Audio Test Certificate",
    [switch]$EnableTestSigning
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell window."
    }
}

function Find-Tool([string]$Name) {
    $fromPath = Get-Command $Name -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }

    $roots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10",
        "$env:ProgramFiles\Windows Kits\10"
    )
    $platformPatterns = @("\\x64\\", "\\x86\\", "\\arm64\\")
    foreach ($root in $roots) {
        if (Test-Path $root) {
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
    }

    throw "$Name was not found. Install the Windows Driver Kit, then retry."
}

function Add-CertToStore($Cert, [string]$StoreName) {
    $store = [System.Security.Cryptography.X509Certificates.X509Store]::new(
        $StoreName,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::LocalMachine
    )
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    try {
        $store.Add($Cert)
    } finally {
        $store.Close()
    }
}

function Find-PackageCertificate([string]$PackageDir) {
    $packagePath = Resolve-Path $PackageDir
    $releaseDir = Split-Path -Parent $packagePath
    $candidates = @(
        (Join-Path $PackageDir "package.cer"),
        (Join-Path $releaseDir "package.cer")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $match = Get-ChildItem -Path $PackageDir, $releaseDir -Filter "*.cer" -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        Select-Object -First 1
    if ($match) {
        return $match.FullName
    }

    return $null
}

function Import-CertificateFile([string]$Path) {
    Write-Host "Trusting driver package certificate:"
    Write-Host "  $Path"
    $cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($Path)
    Add-CertToStore -Cert $cert -StoreName "Root"
    Add-CertToStore -Cert $cert -StoreName "TrustedPublisher"
}

Assert-Admin

$inf = Get-ChildItem -Path $PackageDir -Filter "*.inf" -ErrorAction Stop | Select-Object -First 1
$cat = Get-ChildItem -Path $PackageDir -Filter "*.cat" -ErrorAction Stop | Select-Object -First 1
if (-not $inf -or -not $cat) {
    throw "Could not find INF/CAT files in $PackageDir. Build the driver first."
}

if ($EnableTestSigning) {
    Write-Warning "Enabling Windows test-signing mode can prevent anti-cheat games from launching."
    Write-Warning "Disable test-signing later with Disable-TestSigning.ps1 if you need normal game compatibility."
    Write-Host "Enabling Windows test-signing mode. A reboot is usually required."
    & bcdedit /set testsigning on
}

$packageCertificate = Find-PackageCertificate -PackageDir $PackageDir
if ($packageCertificate) {
    Import-CertificateFile -Path $packageCertificate
} else {
    $cert = Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.Subject -eq "CN=$CertSubject" } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

    if (-not $cert) {
        $cert = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject "CN=$CertSubject" `
            -CertStoreLocation Cert:\LocalMachine\My `
            -KeyAlgorithm RSA `
            -KeyLength 2048 `
            -HashAlgorithm SHA256 `
            -NotAfter (Get-Date).AddYears(5)
    }

    Add-CertToStore -Cert $cert -StoreName "Root"
    Add-CertToStore -Cert $cert -StoreName "TrustedPublisher"

    $signtool = Find-Tool "signtool.exe"
    & $signtool sign /v /fd SHA256 /s My /n $CertSubject $($cat.FullName)
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed with exit code $LASTEXITCODE."
    }
}

$devcon = Find-Tool "devcon.exe"
Write-Host "Adding driver package to the Driver Store..."
& pnputil /add-driver $($inf.FullName)

Write-Host "Installing root-enumerated device $HardwareId..."
& $devcon install $($inf.FullName) $HardwareId
if ($LASTEXITCODE -ne 0) {
    throw "devcon install failed with exit code $LASTEXITCODE. If you just enabled test signing, reboot and rerun this script."
}

Write-Host ""
Write-Host "Installed. In Windows Sound output devices, look for 'Splitter Output'."
Write-Host "In this app, select the matching source, usually 'Loopback: Splitter Output' or 'Input: Splitter Input'."
