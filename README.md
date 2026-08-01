# Audio Splitter

A Windows 10/11 audio splitter with one unified release:

- **Default mode:** no virtual driver install. Use an existing Windows playback device as the loopback source and mirror it to one or two additional outputs.
- **Optional driver mode:** install the branded `Splitter Output` virtual audio endpoint when you want Windows or a game to select a dedicated output device.

The core audio engine is the low-latency immediate splitter: it reads currently available WASAPI loopback audio, skips stale blocks instead of drifting behind, and applies volume changes live.

## Run

Use native Windows Python, not WSL. WSL cannot reliably see Windows audio devices.

Double-click:

```bat
run_windows.bat
```

The launcher opens a small `Launching` window, prepares the Python environment in the background, then opens the splitter UI. The initial console closes after handing off to the launch window.

## No-Driver Mode

This is the fastest path and does not install anything system-level:

1. Set Windows or your game to output to one real device you can already hear, such as your headset.
2. In Audio Splitter, choose that same device as `Capture loopback source`.
3. Set `Additional output A` to the other device, such as Bluetooth earbuds.
4. Leave the captured/source device output set to `None - source device already plays this audio`.

Do not route back into the same device you are capturing from unless you intentionally enable the feedback checkbox. Doing so usually creates doubled, delayed, phasey audio.

## Optional Driver Mode

If you want Windows or a game to show a dedicated output named `Splitter Output`, click `Install Optional Driver` in the app or run:

```bat
setup_windows.bat
```

The optional driver setup builds from the MIT-licensed [VirtualDrivers/Virtual-Audio-Driver](https://github.com/VirtualDrivers/Virtual-Audio-Driver), brands the endpoints as `Splitter Output` and `Splitter Input`, and installs the driver with test signing.

This path requires Visual Studio/Build Tools, Windows SDK, WDK, administrator elevation, and possibly a reboot for Windows test-signing mode. The setup script uses Microsoft's documented WinGet WDK configuration when prerequisites are missing.

After the driver is installed:

1. Set Windows or the game to output to `Splitter Output`.
2. In Audio Splitter, select `Loopback: Splitter Output` as the capture source.
3. Choose the real devices as `Additional output A` and `Additional output B`.

## Latency

- Start with `48000` Hz and block size `512`.
- For lower software latency, try `256` or `128`.
- If you hear crackles, try `1024` or `2048`.
- Use `4096` only if the smaller values are unstable.
- The app skips stale audio blocks so it stays current instead of drifting seconds behind.
- Volume changes apply immediately while routing.
- Source/output/sample-rate/block-size changes automatically restart the live audio stream.

## Volume

Volume sliders go up to `500%`. Boosting quiet audio can help, but already-loud audio is peak-protected to avoid hard clipping.

## Limits

Bluetooth devices add their own unavoidable playback delay. This app avoids software backlog as much as possible, but it cannot remove Bluetooth codec/device latency.
