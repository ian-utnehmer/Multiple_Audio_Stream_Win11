from __future__ import annotations

import math
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk


APP_TITLE = "Audio Splitter"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_BLOCK_SIZE = 512
DEVICE_REFRESH_MS = 1500
LIVE_RESTART_MS = 75
OUTPUT_QUEUE_BLOCKS = 1
STARTUP_DISCARD_SECONDS = 0.12
MAX_VOLUME = 5.0
ERROR_LOG = Path(__file__).with_name("audio_splitter_error.log")
NONE_LABEL = "None - source device already plays this audio"


@dataclass(frozen=True)
class DeviceChoice:
    label: str
    id: str
    name: str
    channels: int
    is_loopback: bool = False
    is_none: bool = False


class AudioRouter:
    def __init__(
        self,
        source: DeviceChoice,
        output_a: DeviceChoice,
        output_b: DeviceChoice,
        sample_rate: int,
        block_size: int,
        volume_a: tk.DoubleVar,
        volume_b: tk.DoubleVar,
    ) -> None:
        self.source = source
        self.output_a = output_a
        self.output_b = output_b
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.volume_a = volume_a
        self.volume_b = volume_b
        self.stop_event = threading.Event()
        self.error_queue: queue.Queue[str] = queue.Queue()
        self.level = 0.0
        self.peak = 0.0
        self.skipped_blocks = 0
        self.frames_routed = 0
        self._thread: threading.Thread | None = None
        self._output_threads: list[threading.Thread] = []
        self._queue_a: queue.Queue[object] = queue.Queue(maxsize=OUTPUT_QUEUE_BLOCKS)
        self._queue_b: queue.Queue[object] = queue.Queue(maxsize=OUTPUT_QUEUE_BLOCKS)
        self._volume_lock = threading.Lock()
        self._volume_a = self._read_volume(volume_a)
        self._volume_b = self._read_volume(volume_b)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="capture-router", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._signal_outputs_to_stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        for thread in self._output_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_volumes(self) -> None:
        with self._volume_lock:
            self._volume_a = self._read_volume(self.volume_a)
            self._volume_b = self._read_volume(self.volume_b)

    def _run(self) -> None:
        try:
            import numpy as np
            import soundcard as sc

            self._initialize_com_for_thread(sc)
            source = sc.get_microphone(id=self.source.id, include_loopback=self.source.is_loopback)
            capture_channels = self._usable_channels(self.source.channels)
            self._start_output_threads()

            with source.recorder(
                samplerate=self.sample_rate,
                channels=capture_channels,
                blocksize=self.block_size,
            ) as recorder:
                self._discard_startup_audio(recorder)
                while not self.stop_event.is_set():
                    data = recorder.record(numframes=None)
                    if data.size == 0:
                        time.sleep(0.001)
                        continue

                    self.level = float(min(1.0, math.sqrt(float(np.mean(np.square(data)))) * 4.0))
                    self.peak = float(min(1.0, np.max(np.abs(data))))
                    self._enqueue_latest(data)
                    self.frames_routed += int(data.shape[0])
        except Exception:
            self._report_error(traceback.format_exc())
        finally:
            self.stop_event.set()
            self._signal_outputs_to_stop()

    def _start_output_threads(self) -> None:
        self._output_threads = []
        if not self.output_a.is_none:
            self._output_threads.append(
                threading.Thread(
                    target=self._play_output,
                    name="output-a",
                    args=(self.output_a, self._queue_a, "A"),
                    daemon=True,
                )
            )
        if not self.output_b.is_none:
            self._output_threads.append(
                threading.Thread(
                    target=self._play_output,
                    name="output-b",
                    args=(self.output_b, self._queue_b, "B"),
                    daemon=True,
                )
            )
        for thread in self._output_threads:
            thread.start()

    def _play_output(self, device: DeviceChoice, output_queue: queue.Queue[object], output_id: str) -> None:
        try:
            import soundcard as sc

            self._initialize_com_for_thread(sc)
            speaker = sc.get_speaker(device.id)
            channels = self._usable_channels(device.channels)

            with speaker.player(
                samplerate=self.sample_rate,
                channels=channels,
                blocksize=self.block_size,
            ) as player:
                while not self.stop_event.is_set():
                    try:
                        item = output_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if item is None:
                        break

                    item = self._drain_to_latest(output_queue, item)
                    player.play(self._for_output(item, channels, self._volume_for(output_id)))
        except Exception:
            if not self.stop_event.is_set():
                self._report_error(f"Output {output_id} failed:\n{traceback.format_exc()}")
            self.stop_event.set()

    def _enqueue_latest(self, data: object) -> None:
        if not self.output_a.is_none:
            self._put_latest(self._queue_a, data)
        if not self.output_b.is_none:
            self._put_latest(self._queue_b, data)

    def _put_latest(self, output_queue: queue.Queue[object], data: object) -> None:
        while True:
            try:
                stale = output_queue.get_nowait()
            except queue.Empty:
                break
            if stale is not None:
                self.skipped_blocks += 1

        try:
            output_queue.put_nowait(data)
        except queue.Full:
            self.skipped_blocks += 1

    def _drain_to_latest(self, output_queue: queue.Queue[object], item: object) -> object:
        while True:
            try:
                newer_item = output_queue.get_nowait()
            except queue.Empty:
                return item
            if newer_item is None:
                self.stop_event.set()
                return item
            item = newer_item
            self.skipped_blocks += 1

    def _signal_outputs_to_stop(self) -> None:
        for output_queue in (self._queue_a, self._queue_b):
            try:
                output_queue.put_nowait(None)
            except queue.Full:
                try:
                    output_queue.get_nowait()
                    output_queue.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass

    def _volume_for(self, output_id: str) -> float:
        with self._volume_lock:
            return self._volume_a if output_id == "A" else self._volume_b

    def _discard_startup_audio(self, recorder: object) -> None:
        deadline = time.perf_counter() + STARTUP_DISCARD_SECONDS
        while not self.stop_event.is_set() and time.perf_counter() < deadline:
            try:
                recorder.record(numframes=None)
            except Exception:
                return

    def _report_error(self, details: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with ERROR_LOG.open("a", encoding="utf-8") as log_file:
                log_file.write(f"\n[{timestamp}]\n{details}\n")
        except Exception:
            pass
        self.error_queue.put(details)

    @staticmethod
    def _initialize_com_for_thread(sc_module: object) -> object | None:
        try:
            mediafoundation = getattr(sc_module, "mediafoundation", None)
            com_library = getattr(mediafoundation, "_COMLibrary", None)
            if com_library is not None:
                return com_library()
        except Exception:
            pass
        return None

    @staticmethod
    def _usable_channels(channels: int) -> int:
        try:
            count = int(channels)
        except Exception:
            return 2
        return 1 if count <= 1 else 2

    @staticmethod
    def _read_volume(volume: tk.DoubleVar) -> float:
        return max(0.0, min(MAX_VOLUME, float(volume.get()) / 100.0))

    @staticmethod
    def _for_output(data, channels: int, volume: float):
        import numpy as np

        routed = np.asarray(data, dtype="float32")
        if routed.ndim == 1:
            routed = routed[:, None]

        if channels == 1 and routed.shape[1] > 1:
            routed = np.mean(routed, axis=1, keepdims=True)
        elif channels > 1 and routed.shape[1] == 1:
            routed = np.tile(routed, (1, channels))
        elif routed.shape[1] > channels:
            routed = routed[:, :channels]
        elif routed.shape[1] < channels:
            pad = np.zeros((routed.shape[0], channels - routed.shape[1]), dtype="float32")
            routed = np.concatenate([routed, pad], axis=1)

        if volume != 1.0:
            routed = routed * volume
        peak = float(np.max(np.abs(routed))) if routed.size else 0.0
        if peak > 1.0:
            routed = routed / peak
        return np.clip(routed, -1.0, 1.0).astype("float32", copy=False)


class AudioSplitterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(800, 520)

        self.sources: list[DeviceChoice] = []
        self.outputs: list[DeviceChoice] = [self._none_output()]
        self.router: AudioRouter | None = None
        self.device_signature: tuple[tuple[str, str, bool], ...] = ()
        self.live_restart_after_id: str | None = None

        self.source_var = tk.StringVar()
        self.output_a_var = tk.StringVar(value=NONE_LABEL)
        self.output_b_var = tk.StringVar(value=NONE_LABEL)
        self.volume_a_var = tk.DoubleVar(value=80)
        self.volume_b_var = tk.DoubleVar(value=80)
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_RATE))
        self.block_size_var = tk.StringVar(value=str(DEFAULT_BLOCK_SIZE))
        self.allow_feedback_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a loopback source and at least one additional output.")
        self.level_var = tk.DoubleVar(value=0)

        self.volume_a_var.trace_add("write", lambda *_args: self._apply_live_volumes())
        self.volume_b_var.trace_add("write", lambda *_args: self._apply_live_volumes())

        self._build_ui()
        self.refresh_devices()
        self.after(120, self._poll_router)
        self.after(DEVICE_REFRESH_MS, self._poll_devices)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.configure(bg="#f5f7fb")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f5f7fb")
        style.configure("Panel.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("TLabel", background="#f5f7fb", foreground="#202532", font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background="#ffffff")
        style.configure("Title.TLabel", background="#f5f7fb", font=("Segoe UI", 18, "bold"))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#5a6272")
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"))

        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh Devices", command=lambda: self.refresh_devices(silent=False)).pack(side="right")
        ttk.Button(top, text="Install Optional Driver", command=self._install_optional_driver).pack(side="right", padx=(0, 8))

        devices = ttk.Frame(root, style="Panel.TFrame", padding=14)
        devices.pack(fill="x", pady=(0, 12))
        self._combo_row(devices, 0, "Capture loopback source", self.source_var, "source_combo")
        self._combo_row(devices, 1, "Additional output A", self.output_a_var, "output_a_combo")
        self._combo_row(devices, 2, "Additional output B", self.output_b_var, "output_b_combo")

        volumes = ttk.Frame(root, style="Panel.TFrame", padding=14)
        volumes.pack(fill="x", pady=(0, 12))
        self._volume_row(volumes, 0, "Output A volume", self.volume_a_var)
        self._volume_row(volumes, 1, "Output B volume", self.volume_b_var)

        settings = ttk.Frame(root, style="Panel.TFrame", padding=14)
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="Sample rate", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        self.sample_rate_combo = ttk.Combobox(
            settings,
            textvariable=self.sample_rate_var,
            values=("44100", "48000"),
            width=10,
            state="readonly",
        )
        self.sample_rate_combo.grid(row=0, column=1, sticky="w", pady=4)
        self.sample_rate_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart("sample rate"))

        ttk.Label(settings, text="Block size", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(24, 12), pady=4)
        self.block_size_combo = ttk.Combobox(
            settings,
            textvariable=self.block_size_var,
            values=("128", "256", "512", "1024", "2048", "4096"),
            width=10,
            state="readonly",
        )
        self.block_size_combo.grid(row=0, column=3, sticky="w", pady=4)
        self.block_size_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart("block size"))

        ttk.Checkbutton(
            settings,
            text="Allow output back into the captured source device",
            variable=self.allow_feedback_var,
            command=lambda: self._schedule_live_restart("feedback setting"),
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        note = (
            "For the cleanest no-driver fallback, set Windows to play through one device you can already hear, "
            "choose that device's Loopback source here, then route only to the other device. "
            "Do not also route back into the captured source unless you need to test it."
        )
        ttk.Label(settings, text=note, style="Hint.TLabel", wraplength=720).grid(
            row=2,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0),
        )

        bottom = ttk.Frame(root)
        bottom.pack(fill="x")
        self.start_button = ttk.Button(bottom, text="Start", style="Start.TButton", command=self.toggle_router)
        self.start_button.pack(side="left")
        ttk.Progressbar(bottom, variable=self.level_var, maximum=100, length=180).pack(side="left", padx=(14, 10))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", fill="x", expand=True)

    def _combo_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, attr_name: str) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
        combo = ttk.Combobox(parent, textvariable=var, state="readonly", width=88)
        combo.grid(row=row, column=1, sticky="ew", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart(label.lower()))
        parent.columnconfigure(1, weight=1)
        setattr(self, attr_name, combo)

    def _volume_row(self, parent: ttk.Frame, row: int, label: str, var: tk.DoubleVar) -> None:
        value = ttk.Label(parent, text=f"{int(var.get())}%", style="Panel.TLabel", width=6)

        def update_value(_event=None) -> None:
            value.configure(text=f"{int(var.get())}%")
            self._apply_live_volumes()

        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
        scale = ttk.Scale(parent, variable=var, from_=0, to=500, command=update_value)
        scale.grid(row=row, column=1, sticky="ew", pady=8)
        value.grid(row=row, column=2, sticky="e", padx=(12, 0), pady=8)
        parent.columnconfigure(1, weight=1)

    def refresh_devices(self, silent: bool = False) -> bool:
        try:
            import soundcard as sc
        except Exception as exc:
            self.status_var.set("Install dependencies first by running run_windows.bat.")
            if not silent:
                messagebox.showerror(APP_TITLE, f"Audio packages are not installed yet.\n\nDetails: {exc}")
            return False

        try:
            speakers = sc.all_speakers()
            default_speaker = sc.default_speaker()
            microphones = sc.all_microphones(include_loopback=True)
        except Exception as exc:
            self.status_var.set("Could not read Windows audio devices.")
            if not silent:
                messagebox.showerror(APP_TITLE, f"Could not read Windows audio devices:\n\n{exc}")
            return False

        old_source = self._selected(self.sources, self.source_var.get())
        old_output_a = self._selected(self.outputs, self.output_a_var.get())
        old_output_b = self._selected(self.outputs, self.output_b_var.get())

        self.sources = []
        seen_sources: set[tuple[str, bool]] = set()
        for microphone in microphones:
            is_loopback = bool(getattr(microphone, "isloopback", False))
            if not is_loopback:
                continue
            key = (microphone.id, is_loopback)
            if key in seen_sources:
                continue
            seen_sources.add(key)
            self.sources.append(
                DeviceChoice(
                    label=f"Loopback: {microphone.name} ({self._channel_text(microphone.channels)})",
                    id=microphone.id,
                    name=microphone.name,
                    channels=int(microphone.channels),
                    is_loopback=True,
                )
            )

        self.outputs = [self._none_output()]
        self.outputs.extend(
            DeviceChoice(
                label=f"{speaker.name} ({self._channel_text(speaker.channels)})",
                id=speaker.id,
                name=speaker.name,
                channels=int(speaker.channels),
            )
            for speaker in speakers
        )

        signature = self._device_signature(self.sources, self.outputs)
        changed = signature != self.device_signature
        self.device_signature = signature

        self.source_combo.configure(values=[device.label for device in self.sources])
        self.output_a_combo.configure(values=[device.label for device in self.outputs])
        self.output_b_combo.configure(values=[device.label for device in self.outputs])

        self._restore_or_default(old_source, self.sources, self.source_var, self._default_source_id(default_speaker.id))
        self._restore_or_default(old_output_a, self.outputs, self.output_a_var, None)
        self._restore_or_default(old_output_b, self.outputs, self.output_b_var, None)

        if not self.router or not self.router.is_alive():
            if changed and silent:
                self.status_var.set(f"Device list updated: {len(self.sources)} loopback source(s), {len(self.outputs) - 1} output(s).")
            else:
                self.status_var.set(f"Found {len(self.sources)} loopback source(s) and {len(self.outputs) - 1} output device(s).")
        return True

    def toggle_router(self) -> None:
        if self.router and self.router.is_alive():
            self._stop_router()
            return
        self._start_router()

    def _start_router(self) -> bool:
        if self.live_restart_after_id:
            self.after_cancel(self.live_restart_after_id)
            self.live_restart_after_id = None

        source = self._selected(self.sources, self.source_var.get())
        output_a = self._selected(self.outputs, self.output_a_var.get())
        output_b = self._selected(self.outputs, self.output_b_var.get())
        if not source or not output_a or not output_b:
            messagebox.showwarning(APP_TITLE, "Choose a source and output choices.")
            return False
        if output_a.is_none and output_b.is_none:
            messagebox.showwarning(APP_TITLE, "Choose at least one additional output.")
            return False
        if not self.allow_feedback_var.get():
            for output in (output_a, output_b):
                if not output.is_none and output.id == source.id:
                    messagebox.showwarning(
                        APP_TITLE,
                        "That output is the same device as the loopback source.\n\n"
                        "That usually causes doubled, phasey, fuzzy audio. Choose None for the source device, "
                        "or enable the checkbox if you really want to test it.",
                    )
                    return False

        try:
            sample_rate = int(self.sample_rate_var.get())
            block_size = int(self.block_size_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Sample rate and block size must be numbers.")
            return False

        self.router = AudioRouter(
            source=source,
            output_a=output_a,
            output_b=output_b,
            sample_rate=sample_rate,
            block_size=block_size,
            volume_a=self.volume_a_var,
            volume_b=self.volume_b_var,
        )
        self.router.start()
        self.start_button.configure(text="Stop")
        self.status_var.set("Routing current audio...")
        return True

    def _stop_router(self, status: str = "Stopped.") -> None:
        if self.live_restart_after_id:
            self.after_cancel(self.live_restart_after_id)
            self.live_restart_after_id = None
        if self.router:
            self.router.stop()
        self.router = None
        self.level_var.set(0)
        self.start_button.configure(text="Start")
        self.status_var.set(status)

    def _schedule_live_restart(self, reason: str) -> None:
        if not self.router or not self.router.is_alive():
            return
        self.status_var.set(f"Applying {reason}...")
        if self.live_restart_after_id:
            self.after_cancel(self.live_restart_after_id)
        self.live_restart_after_id = self.after(LIVE_RESTART_MS, self._restart_router)

    def _restart_router(self) -> None:
        self.live_restart_after_id = None
        if not self.router or not self.router.is_alive():
            return
        self._stop_router(status="Restarting audio...")
        self._start_router()

    def _apply_live_volumes(self) -> None:
        if self.router and self.router.is_alive():
            self.router.set_volumes()

    def _poll_router(self) -> None:
        if self.router:
            while not self.router.error_queue.empty():
                details = self.router.error_queue.get_nowait()
                self._stop_router()
                messagebox.showerror(
                    APP_TITLE,
                    "Audio routing stopped because of an error.\n\n"
                    f"{details}\n\n"
                    f"A copy was written to:\n{ERROR_LOG}",
                )
                break

            if self.router and self.router.is_alive():
                self.level_var.set(self.router.level * 100)
                seconds = self.router.frames_routed / max(1, int(self.sample_rate_var.get()))
                skip_text = f", {self.router.skipped_blocks} stale block(s) skipped" if self.router.skipped_blocks else ""
                peak_text = ", hot input" if self.router.peak > 0.98 else ""
                self.status_var.set(f"Routing current audio... {seconds:,.1f}s processed{skip_text}{peak_text}")
            elif self.router:
                self._stop_router()
        self.after(120, self._poll_router)

    def _poll_devices(self) -> None:
        if self.refresh_devices(silent=True):
            missing = self._missing_active_devices()
            if missing:
                self._stop_router()
                messagebox.showwarning(
                    APP_TITLE,
                    "Routing stopped because a selected device disappeared:\n\n"
                    + "\n".join(f"- {name}" for name in missing),
                )
        self.after(DEVICE_REFRESH_MS, self._poll_devices)

    def _missing_active_devices(self) -> list[str]:
        if not self.router or not self.router.is_alive():
            return []
        missing = []
        if not self._contains_device(self.sources, self.router.source):
            missing.append(self.router.source.label)
        for output in (self.router.output_a, self.router.output_b):
            if not output.is_none and not self._contains_device(self.outputs, output):
                missing.append(output.label)
        return missing

    def _on_close(self) -> None:
        self._stop_router()
        self.destroy()

    def _install_optional_driver(self) -> None:
        setup_script = Path(__file__).with_name("setup_windows.ps1")
        if not setup_script.exists():
            messagebox.showerror(APP_TITLE, "The optional driver setup script was not found.")
            return

        try:
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(setup_script),
                    "-NoStartApp",
                ],
                cwd=str(Path(__file__).resolve().parent),
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start optional driver setup:\n\n{exc}")

    @staticmethod
    def _none_output() -> DeviceChoice:
        return DeviceChoice(label=NONE_LABEL, id="", name=NONE_LABEL, channels=2, is_none=True)

    @staticmethod
    def _selected(devices: list[DeviceChoice], label: str) -> DeviceChoice | None:
        return next((device for device in devices if device.label == label), None)

    @staticmethod
    def _contains_device(devices: list[DeviceChoice], target: DeviceChoice) -> bool:
        return any(
            device.id == target.id and device.is_loopback == target.is_loopback and device.is_none == target.is_none
            for device in devices
        )

    @staticmethod
    def _matching_device(devices: list[DeviceChoice], target: DeviceChoice) -> DeviceChoice:
        return next(
            device
            for device in devices
            if device.id == target.id and device.is_loopback == target.is_loopback and device.is_none == target.is_none
        )

    def _restore_or_default(
        self,
        old_device: DeviceChoice | None,
        devices: list[DeviceChoice],
        variable: tk.StringVar,
        preferred_id: str | None,
    ) -> None:
        if old_device and self._contains_device(devices, old_device):
            variable.set(self._matching_device(devices, old_device).label)
            return
        if variable.get() in [device.label for device in devices]:
            return
        if preferred_id:
            matches = [device for device in devices if device.id == preferred_id]
            if matches:
                variable.set(matches[0].label)
                return
        variable.set(devices[0].label if devices else "")

    def _default_source_id(self, default_speaker_id: str) -> str | None:
        for source in self.sources:
            if source.id == default_speaker_id:
                return source.id
        return None

    @staticmethod
    def _device_signature(sources: list[DeviceChoice], outputs: list[DeviceChoice]) -> tuple[tuple[str, str, bool, bool], ...]:
        rows = [(device.id, device.label, device.is_loopback, device.is_none) for device in sources + outputs]
        return tuple(sorted(rows))

    @staticmethod
    def _channel_text(channels: int) -> str:
        return "1 channel" if int(channels) == 1 else f"{int(channels)} channels"


if __name__ == "__main__":
    AudioSplitterApp().mainloop()
