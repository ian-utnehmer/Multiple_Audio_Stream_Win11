# Dual Output Router

A small Windows Python app that captures an audio stream and plays it to two different output devices with independent volume controls.

## What this can do

- Pick a capture source, usually a Windows WASAPI loopback source.
- Pick two different playback devices, such as a wired headset and Bluetooth earbuds.
- Control each routed output's volume separately.
- Keep the device lists current while the app is open.
- Stop routing with a warning if a selected device disconnects.
- Run without a custom audio driver.

## Important Windows audio note

Windows does not let a normal app create a selectable playback device by itself. To make a "some audio source" output appear in Windows Sound settings or in a game's audio output list, you need a virtual audio driver/endpoint such as VB-CABLE, Virtual Audio Cable, or a similar tool.

Once that virtual endpoint exists, this app can use it as the source and route its audio to two real devices.

If you capture the loopback of the same device you are also playing into, you can create echo or feedback.

## Use it as a Windows/game output

For true two-bus control, use a separate capture endpoint:

1. Install a virtual audio endpoint such as VB-CABLE.
2. Set Windows, or the game/app you care about, to play into the virtual cable playback device. In VB-CABLE this is usually named `CABLE Input`.
3. In Dual Output Router, choose the matching virtual cable recording source. In VB-CABLE this is usually named `Input: CABLE Output`.
4. Choose your headset as Output A and earbuds as Output B.
5. Use the two volume sliders independently.

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

- Start with `48000` sample rate and `1024` block size.
- If audio crackles, try block size `2048` or `4096`.
- Bluetooth devices add latency. Two Bluetooth devices may drift slightly over time because each has its own hardware clock.
- Some DRM-protected app audio may not appear in loopback capture.
