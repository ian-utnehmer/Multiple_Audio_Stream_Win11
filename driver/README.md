# Splitter Virtual Audio Driver

This folder packages a simple branded virtual audio endpoint for this project.

It uses the open-source [VirtualDrivers/Virtual-Audio-Driver](https://github.com/VirtualDrivers/Virtual-Audio-Driver) project, pinned to tag `25.7.14`, and patches the installer strings so Windows shows:

- `Splitter Output` as the playback/output device
- `Splitter Input` as the recording/input side

## Reality Check

Windows 11 will only show `Splitter Output` as a selectable app/game output if a driver exposes that endpoint. A Python app cannot create that endpoint by itself.

This is a test-signed driver flow. That means:

- Install Visual Studio 2022 or Build Tools.
- Install the Windows SDK and Windows Driver Kit.
- Run install scripts from elevated PowerShell.
- Enable Windows test-signing mode and reboot if needed.
- Secure Boot may prevent test-signing mode until it is disabled in firmware.

This is not a production-signed driver package. Shipping a production driver requires Microsoft driver signing.

## Build And Install

From a normal PowerShell:

```powershell
cd D:\SoundProject
.\driver\Prepare-SplitterDriver.ps1
.\driver\Build-SplitterDriver.ps1
```

From an elevated PowerShell:

```powershell
cd D:\SoundProject
.\driver\Install-SplitterDriver.ps1 -EnableTestSigning
```

If test-signing was just enabled, reboot, then rerun:

```powershell
cd D:\SoundProject
.\driver\Install-SplitterDriver.ps1
```

After installation:

1. Open Windows Sound settings.
2. Pick `Splitter Output` as the system output or as a specific game's output.
3. Open Dual Output Router.
4. Select `Loopback: Splitter Output` first. If that does not carry real audio, try `Input: Splitter Input`.
5. Select your headset and earbuds as Output A and Output B.

## Uninstall

From an elevated PowerShell:

```powershell
cd D:\SoundProject
.\driver\Uninstall-SplitterDriver.ps1
```

## Next Driver Step

If `Splitter Output` installs but its loopback/capture side does not carry the real game stream, the next implementation step is inside the driver: add a shared ring buffer so render frames written to `Splitter Output` are readable from `Splitter Input`. The scripts here intentionally make that work reproducible before we start deeper kernel-mode changes.
