# Audio Splitter

Audio Splitter is a Windows 10/11 app for mirroring one live audio stream to multiple playback devices with independent per-output volume controls.

It has one unified release with two modes:

- **No-driver mode:** the default path. Capture an existing Windows playback device through WASAPI loopback and mirror it to as many additional outputs as your PC can handle.
- **Optional-driver mode:** installs a locally built virtual endpoint named `Splitter Output` so Windows, games, or individual apps can choose that endpoint directly.

The main audio engine is designed to stay current: it reads fixed-size loopback blocks, hands only one block at a time to each output, resyncs instead of building app-side delay, and applies per-output volume changes immediately.

The default interface is a React control surface served by a local Python host. The same host owns the Windows audio routing engine, so the UI can update device choices, volumes, and routing state without replacing the low-latency audio path. A native CustomTkinter fallback is also included.

## Motivation

Windows Volume Mixer can choose an app's output device and app volume, but Windows does not include a small built-in tool for mirroring one playback stream to multiple real output devices with separate volume sliders for each output.

[Voicemeeter](https://vb-audio.com/Voicemeeter/) is a powerful and actively maintained virtual mixer. Its Banana and Potato editions support multiple outputs and bus controls. This project is intentionally narrower: it is a lightweight splitter UI focused on quickly sending the same current audio to any number of selected outputs, each with its own instant volume control.

This app also came from frustration with the driver-heavy setup and cleanup process around virtual mixer tools. In my own testing, uninstalling and reinstalling Voicemeeter multiple times still left Windows believing pieces were installed, including old virtual drivers/cables that were not removed cleanly. Audio Splitter keeps the default path driver-free and makes the virtual-driver path optional and explicit.

## Features

- Mirror one audio source to any number of selected playback outputs.
- Add and remove output rows dynamically.
- Control each output row independently from `0%` to `500%`.
- Apply volume changes instantly while routing.
- Restart the live stream automatically when source, output, sample rate, or block size changes.
- Keep the device list refreshed while the app is open.
- Refresh devices in the background without rebuilding selectors unless the actual device list changes.
- Stop routing if a selected device disappears.
- Use a modern React UI with styled scrollbars, segmented controls, and responsive routing controls.
- Avoid app-side backlog with a one-block live handoff per output.
- Provide a no-driver default mode for the cleanest day-to-day use.
- Provide an optional virtual driver mode for a selectable Windows endpoint named `Splitter Output`.

## Requirements

For normal no-driver use:

- Windows 10 or Windows 11
- Native Windows Python 3
- Windows audio devices visible to WASAPI
- Python packages from `requirements.txt`: `soundcard`, `numpy`, `customtkinter`, and `pywebview`

Do not run the app from WSL. WSL cannot reliably see or route native Windows audio devices.

For optional-driver use:

- Administrator access
- Visual Studio 2022 or Build Tools
- Windows SDK
- Windows Driver Kit
- Windows test-signing mode enabled
- Secure Boot disabled while using the locally test-signed driver

The optional driver is a locally test-signed development driver. A production-ready driver requires Microsoft driver signing through the Windows Hardware Developer Program.

## Quick Start

Double-click:

```bat
run_windows.bat
```

The launcher opens a small `Launching` window, prepares the Python environment, installs dependencies into `.venv`, and opens the app. The console window closes after handing off to the launcher UI.

By default, the app opens the React interface in an embedded desktop window through `pywebview`. The local Python host still owns the audio engine, but the UI should look and behave like an app, not a browser tab. A browser mode exists only as an explicit developer fallback with `react_host.py --browser`.

If the app window does not appear, check `launcher_error.log` or `react_host_error.log` in the project folder.

To launch the native fallback UI instead, double-click:

```bat
run_native_windows.bat
```

## No-Driver Mode

Use this when you want the simplest and most reliable setup.

1. Set Windows or your game to output to one real device you can already hear, such as your headset.
2. Open Audio Splitter.
3. Set `Capture loopback source` to that same device's loopback source.
4. Set an additional output row to another device, such as Bluetooth earbuds.
5. Click `Add Output` for any extra playback devices.
6. Leave the captured/source device's own output row set to `None - source device already plays this audio`.
7. Click `Start`.

Do not route audio back into the same device you are capturing from unless you intentionally enable the feedback checkbox. Routing back into the captured source usually creates doubled, delayed, phasey, or fuzzy audio.

## Optional Driver Mode

Use this when you need Windows, a game, or a specific app to show a dedicated output named `Splitter Output`.

Install from inside the app:

1. Run `run_windows.bat`.
2. Click `Install Optional Driver`.
3. Approve the administrator prompt.
4. Let setup build and install the driver.
5. Reboot if setup enables test-signing or asks you to reboot.

Or install directly:

```bat
setup_windows.bat
```

After installation:

1. Open Windows Sound settings.
2. Set the system output, game output, or app output to `Splitter Output`.
3. Open Audio Splitter.
4. Set `Capture loopback source` to `Loopback: Splitter Output`.
5. If that does not carry audio, try `Input: Splitter Input`.
6. Add one output row per real playback device.
7. Click `Start`.

## App Controls

- `Capture loopback source`: the Windows audio stream to mirror.
- `Additional outputs`: dynamic output rows. Each row has its own playback device and volume slider.
- `Add Output`: creates another output row.
- `Remove`: deletes an output row.
- `Main output volume`: master volume for the entire routed stream, from `0%` to `100%`.
- `Output volume`: per-row volume from `0%` to `500%`.
- `Sample rate`: `44100` or `48000` Hz.
- `Block size`: lower values reduce software latency; higher values are more tolerant of unstable devices.
- `Allow output back into the captured source device`: disables the feedback protection guard for testing.
- `Refresh Devices`: manually refreshes the device list.
- `Install Optional Driver`: starts the optional virtual-driver setup.
- Main workspace and output rows are scrollable when the window is smaller or many outputs are added.
- `Quit App`: closes the React host.

## Latency And Audio Quality

Recommended starting point:

- Sample rate: `48000`
- Block size: `512`

Tuning:

- Try `128` or `64` for lower software latency.
- Try `1024` or `2048` if you hear crackles.
- Use `4096` only if smaller values are unstable.
- Bluetooth devices add hardware/codec delay that software cannot remove.
- Avoid selecting the same physical device as both the capture source and an additional output.

The app uses a one-block live handoff per output instead of a deep software buffer. It never intentionally queues a long backlog. If an output device cannot accept the next block in time, the pending block is dropped as a resync so playback stays current instead of becoming seconds late.

## Volume Behavior

`Main output volume` is the master bus for the app. Lower it when you want every routed device to get quieter together.

Each output row has an independent volume slider up to `500%`.

When using the optional virtual driver, changing Windows' volume for `Splitter Output` may not affect the audio captured and mirrored by the app. Use `Main output volume` inside Audio Splitter for reliable overall routed volume control.

Boosting a per-output row above `100%` can help quiet streams, but it cannot recover detail from audio that is already clipped or distorted. The app applies peak protection so boosted audio does not hard-clip above full scale.

## Device Changes

The app refreshes Windows audio devices while it is open. Background scans are quiet: selectors are not rebuilt unless Windows reports that the actual device list changed. If a selected source or output disconnects, routing stops and the app tells you which device disappeared. Reconnect the device, click `Refresh Devices` if needed, then start routing again.

## Optional Driver Details

The optional driver setup builds from the MIT-licensed [VirtualDrivers/Virtual-Audio-Driver](https://github.com/VirtualDrivers/Virtual-Audio-Driver), currently pinned to tag `25.7.14` in the setup script, and brands the endpoints as:

- `Splitter Output`
- `Splitter Input`

Important driver notes:

- A normal Python app cannot create a real Windows playback endpoint by itself. Windows needs an audio driver for `Splitter Output` to appear as a selectable playback device.
- The included driver flow is for local testing and development.
- Windows test-signed kernel drivers require test-signing mode.
- Secure Boot can block test-signing mode.
- A public production driver should be Microsoft-signed.

See [driver/README.md](driver/README.md) for the driver-specific build, install, uninstall, and troubleshooting notes.

## Troubleshooting

### `Splitter Output` does not appear in Windows

- Reboot once after driver installation.
- Open Windows Sound settings and check output devices.
- Open Device Manager and check whether `Splitter Audio Cable` is present.
- Rerun `setup_windows.bat` from the latest repo state.

### `Splitter Output` appears but no audio is captured

- In Windows Sound settings, make sure the system/game/app output is set to `Splitter Output`.
- In Audio Splitter, try `Loopback: Splitter Output`.
- If loopback is silent, try `Input: Splitter Input`.
- Click `Refresh Devices` after changing Windows output devices.

### Audio sounds fuzzy, phasey, or doubled

- Do not route back into the same physical device you are capturing.
- In no-driver mode, let the source device play normally and route only to the other devices.
- Lower `Main output volume` if the input is already hot.
- Try `48000` Hz and block size `512`, then adjust from there.

### Audio is delayed

- Use a smaller block size such as `128` or `64`.
- Avoid Bluetooth if you need very low latency.
- Watch for resync counts in the status line; frequent resyncs mean that device, sample rate, block size, or CPU load cannot keep up cleanly.

### Driver setup says test-signing is blocked

Secure Boot is likely enabled. Disable Secure Boot in UEFI/BIOS, boot back into Windows, and rerun setup. If BitLocker or Device Encryption is enabled, save your recovery key before changing Secure Boot settings.

### Driver build mentions `InfVerif.dll` or `ApiValidator.exe`

The setup script handles a known WDK validation-tool failure by retrying without validation-only targets and creating the driver catalog with `inf2cat.exe`.

## Repository Layout

- `audio_splitter.py`: native CustomTkinter fallback UI and low-latency audio router
- `react_host.py`: local HTTP API/static host for the React interface
- `launch_ui.py`: splash launcher that prepares `.venv` and opens the app
- `run_windows.bat`: normal Windows launcher
- `run_native_windows.bat`: native fallback launcher
- `setup_windows.bat`: direct optional-driver setup launcher
- `setup_windows.ps1`: elevated setup orchestration
- `requirements.txt`: Python dependencies
- `web/`: React source and committed production build
- `assets/audio_splitter.ico`: Windows window/taskbar icon
- `web/public/audio_splitter.ico`: React app-window/favicon icon copied into the production build
- `driver/`: optional virtual audio driver scripts and documentation

Generated local files are ignored:

- `.venv/`
- `__pycache__/`
- `web/node_modules/`
- `*.log`
- `driver/work/`
- `driver/out/`
- `driver/.cache/`

## Development

Create or update the environment by running:

```bat
run_windows.bat
```

For a quick syntax check from a developer shell:

```powershell
py -3 -m py_compile audio_splitter.py launch_ui.py
```

The app depends on:

- `soundcard`
- `numpy`

## Current External References

- [Windows Volume Mixer and app audio routing](https://support.microsoft.com/en-us/windows/hardware/audio/fix-app-audio-not-working-while-system-sounds-work-in-windows)
- [VB-Audio Voicemeeter](https://vb-audio.com/Voicemeeter/)
- [VB-Audio Voicemeeter Banana](https://vb-audio.com/Voicemeeter/banana.htm)
- [VB-Audio Voicemeeter Potato](https://vb-audio.com/Voicemeeter/potato.htm)
- [Microsoft: Loading test-signed code](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/the-testsigning-boot-configuration-option)
- [Microsoft: Driver signing options](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/driver-signing-offerings)
- [Microsoft: Attestation sign Windows drivers](https://learn.microsoft.com/en-us/windows-hardware/drivers/dashboard/code-signing-attestation)
