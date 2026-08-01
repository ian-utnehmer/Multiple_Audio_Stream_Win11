# Dual Output Router

A small Windows Python app that captures an audio stream and plays it to two different output devices with independent volume controls.

## What this can do

- Pick a capture source, usually a Windows WASAPI loopback source.
- Pick two different playback devices, such as a wired headset and Bluetooth earbuds.
- Control each routed output's volume separately.
- Apply volume slider changes immediately while routing.
- Restart the audio stream automatically when source, output, sample rate, or block size changes.
- Keep the device lists current while the app is open.
- Stop routing with a warning if a selected device disconnects.
- Route each output through its own playback worker and skip stale audio blocks so output stays live instead of drifting behind.
- Boost each output up to 500%, with clean peak protection when the boosted signal would clip.
- Run without a custom audio driver.

## Important Windows audio note

Windows does not let a normal app create a selectable playback device by itself. To make a "some audio source" output appear in Windows Sound settings or in a game's audio output list, you need a virtual audio driver/endpoint such as VB-CABLE, Virtual Audio Cable, or a similar tool.

Once that virtual endpoint exists, this app can use it as the source and route its audio to two real devices.

Creating a native Windows playback device named something like `SPLITTER OUTPUT` is possible, but it is a driver project, not a small Python/Tkinter app feature. It would need a Windows virtual audio driver, an INF installer, and driver signing before Windows 11 will treat it like a real selectable output device.

This repo now includes a first-pass open-source driver package under `driver/`. It builds from the MIT-licensed VirtualDrivers/Virtual-Audio-Driver project and brands the endpoints as `Splitter Output` and `Splitter Input`. See [driver/README.md](driver/README.md).

If you capture the loopback of the same device you are also playing into, you can create echo or feedback.

## Use it as a Windows/game output

For true two-bus control, use a separate capture endpoint:

1. Install a virtual audio endpoint such as VB-CABLE.
2. Set Windows, or the game/app you care about, to play into the virtual cable playback device. In VB-CABLE this is usually named `CABLE Input`.
3. In Dual Output Router, choose the matching virtual cable recording source. In VB-CABLE this is usually named `Input: CABLE Output`.
4. Choose your headset as Output A and earbuds as Output B.
5. Use the two volume sliders independently.

With this repo's driver package, use `Splitter Output` as the Windows/game output, then select `Loopback: Splitter Output` in Dual Output Router. If the driver's capture side works better on your machine, select `Input: Splitter Input` instead.

Without a virtual endpoint, you can still mirror audio from one existing playback device to another, but the original device's volume is controlled by Windows or the source app.

## Device changes

The app rescans Windows audio devices every two seconds. Plugging in a headset, connecting Bluetooth earbuds, or disconnecting either should update the dropdowns automatically. If a selected source or output disappears while audio is routing, the app stops routing and shows which device went missing.

## Run on Windows 10 or Windows 11

Use native Windows Python, not WSL. WSL cannot reliably see your Windows audio devices.

Double-click:

```bat
run_windows.bat
```

Or run manually from PowerShell:

```powershell
cd D:\SoundProject
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\dual_output_router.py
```

## Tips

- Start with `48000` sample rate and `4096` block size.
- If audio crackles, try block size `8192`. Smaller blocks reduce latency, but `512` is intentionally not offered because it is usually too unstable for this routing approach.
- The app allows output volume up to 500%. Boosting above 100% cannot make already-full-scale audio cleaner, so very loud sources may not get much louder.
- If the status line says `stale block(s) skipped`, the app is staying live by discarding delayed audio. Try a larger block size or reconnect the Bluetooth device if you hear gaps.
- Bluetooth devices add latency. Two Bluetooth devices may drift slightly over time because each has its own hardware clock.
- Some DRM-protected app audio may not appear in loopback capture.
