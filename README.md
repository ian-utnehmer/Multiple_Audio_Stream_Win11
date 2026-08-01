# Dual Output Router

A small Windows Python app that captures an audio stream and plays it to two different output devices with independent volume controls.

## What this can do

- Pick a capture source, usually a Windows WASAPI loopback source.
- Pick two different playback devices, such as a wired headset and Bluetooth earbuds.
- Control each routed output's volume separately.
- Run without a custom audio driver.

## Important Windows audio note

Windows does not expose one neutral "all system audio" stream that can be freely mixed to multiple outputs. If you capture the loopback of the same device you are also playing into, you can create echo or feedback.

For true two-bus control, use a separate capture endpoint:

1. Install a virtual audio endpoint such as VB-CABLE.
2. Set Windows, or the app you care about, to play into the virtual cable input.
3. In Dual Output Router, choose the virtual cable loopback as the capture source.
4. Choose your headset as Output A and earbuds as Output B.
5. Use the two volume sliders independently.

Without a virtual endpoint, you can still mirror audio from one existing playback device to another, but the original device's volume is controlled by Windows or the source app.

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
