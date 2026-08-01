[CmdletBinding()]
param(
    [switch]$StartMenu,
    [switch]$Taskbar,
    [switch]$All
)

$ErrorActionPreference = "Stop"

if (-not $StartMenu -and -not $Taskbar -and -not $All) {
    $All = $true
}
if ($All) {
    $StartMenu = $true
    $Taskbar = $true
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = "Audio Splitter"
$Icon = Join-Path $Root "assets\audio_splitter.ico"
$Launcher = Join-Path $Root "launch_ui.py"
$RunBatch = Join-Path $Root "run_windows.bat"
$Messages = New-Object System.Collections.Generic.List[string]

function New-AudioSplitterShortcut {
    param([Parameter(Mandatory = $true)][string]$Path)

    $ShortcutShell = New-Object -ComObject WScript.Shell
    $Shortcut = $ShortcutShell.CreateShortcut($Path)
    $Pyw = Get-Command "pyw.exe" -ErrorAction SilentlyContinue

    if ($Pyw) {
        $Shortcut.TargetPath = $Pyw.Source
        $Shortcut.Arguments = "-3 `"$Launcher`""
    } else {
        $Shortcut.TargetPath = $RunBatch
        $Shortcut.Arguments = ""
    }

    $Shortcut.WorkingDirectory = $Root
    $Shortcut.Description = "Open Audio Splitter"
    if (Test-Path $Icon) {
        $Shortcut.IconLocation = "$Icon,0"
    }
    $Shortcut.Save()
}

$Result = [ordered]@{
    startMenu = $false
    taskbar = $false
    taskbarPinned = $false
    startMenuPath = $null
    taskbarPath = $null
    messages = $Messages
}

if ($StartMenu) {
    $StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Audio Splitter"
    New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null
    $StartMenuShortcut = Join-Path $StartMenuDir "$Name.lnk"
    New-AudioSplitterShortcut -Path $StartMenuShortcut
    $Result.startMenu = $true
    $Result.startMenuPath = $StartMenuShortcut
    $Messages.Add("Start Menu shortcut created.")
}

if ($Taskbar) {
    $TaskbarDir = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
    New-Item -ItemType Directory -Force -Path $TaskbarDir | Out-Null
    $TaskbarShortcut = Join-Path $TaskbarDir "$Name.lnk"
    New-AudioSplitterShortcut -Path $TaskbarShortcut
    $Result.taskbar = $true
    $Result.taskbarPath = $TaskbarShortcut
    $Messages.Add("Taskbar shortcut file created.")

    try {
        $PinSource = if ($Result.startMenuPath) { $Result.startMenuPath } else { $TaskbarShortcut }
        $ShellApplication = New-Object -ComObject Shell.Application
        $Folder = $ShellApplication.Namespace((Split-Path $PinSource -Parent))
        $Item = $Folder.ParseName((Split-Path $PinSource -Leaf))
        $Verb = $Item.Verbs() | Where-Object { $_.Name.Replace("&", "") -match "Pin to taskbar" } | Select-Object -First 1
        if ($Verb) {
            $Verb.DoIt()
            $Result.taskbarPinned = $true
            $Messages.Add("Taskbar pin request sent to Windows.")
        } else {
            $Messages.Add("Windows did not expose a taskbar pin command. If it is not pinned, pin Audio Splitter from Start.")
        }
    } catch {
        $Messages.Add("Windows blocked automatic taskbar pinning. Pin Audio Splitter from Start if needed.")
    }
}

$Result | ConvertTo-Json -Depth 4
