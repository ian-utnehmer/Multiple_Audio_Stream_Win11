[CmdletBinding()]
param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-JsonResult {
    param([hashtable]$Result)
    $Result | ConvertTo-Json -Depth 4
}

if (-not (Test-Admin)) {
    $Args = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$PSCommandPath`""
    )
    if ($NoPause) {
        $Args += "-NoPause"
    }

    Start-Process powershell.exe -Verb RunAs -ArgumentList $Args
    Write-JsonResult @{
        ok = $true
        elevated = $true
        rebootRequired = $true
        messages = @(
            "Administrator approval requested.",
            "After the elevated window finishes, restart Windows.",
            "Disabling test-signing can allow anti-cheat games to launch again, but the local Splitter Output test driver may stop loading."
        )
    }
    exit 0
}

$Messages = New-Object System.Collections.Generic.List[string]
$Commands = @(
    @{ label = "test-signing"; args = @("/set", "testsigning", "off") },
    @{ label = "kernel debugging"; args = @("/debug", "off") },
    @{ label = "integrity-check bypass"; args = @("/set", "nointegritychecks", "off") }
)

foreach ($Command in $Commands) {
    $Output = & bcdedit @($Command.args) 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        $Messages.Add("Disabled $($Command.label).")
    } else {
        $Messages.Add("Could not change $($Command.label): $($Output.Trim())")
    }
}

$Messages.Add("Restart Windows for these boot-setting changes to take effect.")
$Messages.Add("If Splitter Output stops loading afterward, use no-driver mode or reinstall/re-enable the optional test driver.")

Write-JsonResult @{
    ok = $true
    elevated = $false
    rebootRequired = $true
    messages = $Messages
}

if (-not $NoPause) {
    Write-Host ""
    Read-Host "Press Enter to close"
}
