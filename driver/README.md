# Optional Splitter Virtual Audio Driver

This folder packages the optional virtual audio endpoint for Audio Splitter.

The main app does not require this driver. Use the driver only when you want Windows, a game, or another app to show a dedicated playback device named `Splitter Output`.

The driver package uses the open-source [VirtualDrivers/Virtual-Audio-Driver](https://github.com/VirtualDrivers/Virtual-Audio-Driver) project, pinned to tag `25.7.14`, and patches the installer strings so Windows shows:

- `Splitter Output` as the playback/output device
- `Splitter Input` as the recording/input side

## Reality Check

Windows 11 will only show `Splitter Output` as a selectable app/game output if a driver exposes that endpoint. A Python app can capture an existing loopback device, but it cannot create a real Windows playback endpoint by itself.

This is a test-signed driver flow. That means:

- Install Visual Studio 2022 or Build Tools.
- Install the Windows SDK and Windows Driver Kit.
- Run install scripts from elevated PowerShell.
- Enable Windows test-signing mode and reboot if needed.
- Secure Boot may prevent test-signing mode until it is disabled in firmware.

This is not a production-signed driver package. Shipping a production driver requires Microsoft driver signing.

## Build And Install

The simplest path is from the app:

1. Run `run_windows.bat`.
2. Click `Install Optional Driver`.
3. Approve the administrator prompt.
4. Let setup finish. If it enables test-signing, reboot and run the setup again.

You can also run driver setup directly:

```bat
setup_windows.bat
```

Or from PowerShell:

```powershell
.\setup_windows.ps1 -NoStartApp
```

After installation:

1. Open Windows Sound settings.
2. Pick `Splitter Output` as the system output or as a specific game's output.
3. Open Audio Splitter.
4. Select `Loopback: Splitter Output` first. If that does not carry real audio, try `Input: Splitter Input`.
5. Add one output row per real playback device you want to mirror to.

## Uninstall

From an elevated PowerShell:

```powershell
.\driver\Uninstall-SplitterDriver.ps1
```

## Driver Development Note

If `Splitter Output` installs but its loopback/capture side does not carry the real game stream, the next implementation step is inside the driver: add a shared ring buffer so render frames written to `Splitter Output` are readable from `Splitter Input`.
