from __future__ import annotations

import math
import queue
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable


APP_TITLE = "Dual Output Router"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_BLOCK_SIZE = 4096
DEVICE_REFRESH_MS = 2000
OUTPUT_QUEUE_BLOCKS = 8
MAX_VOLUME = 5.0
ERROR_LOG = Path(__file__).with_name("router_error.log")
SPLITTER_SOURCE_NAMES = ("Splitter Input", "Splitter Output")


@dataclass(frozen=True)
class DeviceChoice:
    label: str
    id: str
    name: str
    channels: int
    kind: str
    is_loopback: bool = False


class AudioRouter:
    def __init__(
        self,
        source: DeviceChoice,
        output_a: DeviceChoice,
        output_b: DeviceChoice,
        sample_rate: int,
        block_size: int,
        volume_a: Callable[[], float],
        volume_b: Callable[[], float],
    ) -> None:
        self.source = source
        self.output_a = output_a
        self.output_b = output_b
        self.sample_rate = sample_rate
        self.block_size = block_size
        self._initial_volume_a = volume_a
        self._initial_volume_b = volume_b
        self.stop_event = threading.Event()
        self.error_queue: queue.Queue[str] = queue.Queue()
        self.level = 0.0
        self.peak = 0.0
        self.dropped_blocks = 0
        self.frames_routed = 0
        self._thread: threading.Thread | None = None
        self._output_threads: list[threading.Thread] = []
        self._queue_a: queue.Queue[object] = queue.Queue(maxsize=OUTPUT_QUEUE_BLOCKS)
        self._queue_b: queue.Queue[object] = queue.Queue(maxsize=OUTPUT_QUEUE_BLOCKS)
        self._volume_lock = threading.Lock()
        self._volume_a = max(0.0, min(MAX_VOLUME, self._initial_volume_a()))
        self._volume_b = max(0.0, min(MAX_VOLUME, self._initial_volume_b()))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-router", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._signal_outputs_to_stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.5)
        for thread in self._output_threads:
            if thread.is_alive():
                thread.join(timeout=2.5)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_volumes(self, volume_a: float, volume_b: float) -> None:
        with self._volume_lock:
            self._volume_a = max(0.0, min(MAX_VOLUME, float(volume_a)))
            self._volume_b = max(0.0, min(MAX_VOLUME, float(volume_b)))

    def _run(self) -> None:
        try:
            import numpy as np
            import soundcard as sc

            thread_com = self._initialize_com_for_thread(sc)

            source = sc.get_microphone(id=self.source.id, include_loopback=self.source.is_loopback)

            capture_channels = self._usable_channels(self.source.channels)
            self._start_output_threads()

            with source.recorder(
                samplerate=self.sample_rate,
                channels=capture_channels,
                blocksize=self.block_size,
            ) as recorder:
                while not self.stop_event.is_set():
                    data = recorder.record(numframes=self.block_size)
                    if data.size == 0:
                        time.sleep(0.002)
                        continue

                    self.level = float(min(1.0, math.sqrt(float(np.mean(np.square(data)))) * 4.0))
                    self.peak = float(min(1.0, np.max(np.abs(data))))
                    self._enqueue_for_outputs(data)
                    self.frames_routed += int(data.shape[0])
        except Exception:
            self._report_error(traceback.format_exc())
        finally:
            self.stop_event.set()
            self._signal_outputs_to_stop()

    def _start_output_threads(self) -> None:
        self._output_threads = [
            threading.Thread(
                target=self._play_output,
                name="audio-output-a",
                args=(self.output_a, self._queue_a, "Output A"),
                daemon=True,
            ),
            threading.Thread(
                target=self._play_output,
                name="audio-output-b",
                args=(self.output_b, self._queue_b, "Output B"),
                daemon=True,
            ),
        ]
        for thread in self._output_threads:
            thread.start()

    def _play_output(self, device: DeviceChoice, output_queue: queue.Queue[object], label: str) -> None:
        try:
            import soundcard as sc

            thread_com = self._initialize_com_for_thread(sc)
            speaker = sc.get_speaker(device.id)
            channels = self._usable_channels(device.channels)

            with speaker.player(
                samplerate=self.sample_rate,
                channels=channels,
                blocksize=self.block_size,
            ) as player:
                while not self.stop_event.is_set():
                    try:
                        item = output_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if item is None:
                        break

                    volume = self._volume_for_label(label)
                    player.play(self._for_output(item, channels, volume))
        except Exception:
            if not self.stop_event.is_set():
                self._report_error(f"{label} failed:\n{traceback.format_exc()}")
            self.stop_event.set()

    def _enqueue_for_outputs(self, data: object) -> None:
        self._put_latest(self._queue_a, data)
        self._put_latest(self._queue_b, data)

    def _put_latest(self, output_queue: queue.Queue[object], data: object) -> None:
        try:
            output_queue.put_nowait(data)
        except queue.Full:
            self.dropped_blocks += 1
            try:
                output_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                output_queue.put_nowait(data)
            except queue.Full:
                pass

    def _signal_outputs_to_stop(self) -> None:
        for output_queue in (self._queue_a, self._queue_b):
            try:
                output_queue.put_nowait(None)
            except queue.Full:
                try:
                    output_queue.get_nowait()
                    output_queue.put_nowait(None)
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    def _volume_for_label(self, label: str) -> float:
        with self._volume_lock:
            return self._volume_a if label == "Output A" else self._volume_b

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
            # The soundcard import usually initializes COM already. This is just
            # an extra nudge for Windows worker threads.
            pass
        return None

    @staticmethod
    def _usable_channels(channels: int) -> int:
        try:
            count = int(channels)
        except Exception:
            return 2
        if count <= 1:
            return 1
        return 2

    @staticmethod
    def _for_output(data, channels: int, volume: float):
        import numpy as np

        volume = max(0.0, min(MAX_VOLUME, float(volume)))
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
            routed = np.tanh(routed)
        return np.clip(routed, -1.0, 1.0).astype("float32", copy=False)


class DualOutputApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(780, 540)

        self.sources: list[DeviceChoice] = []
        self.outputs: list[DeviceChoice] = []
        self.router: AudioRouter | None = None
        self.device_signature: tuple[tuple[str, str, bool], ...] = ()

        self.source_var = tk.StringVar()
        self.output_a_var = tk.StringVar()
        self.output_b_var = tk.StringVar()
        self.volume_a_var = tk.DoubleVar(value=80)
        self.volume_b_var = tk.DoubleVar(value=80)
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_RATE))
        self.block_size_var = tk.StringVar(value=str(DEFAULT_BLOCK_SIZE))
        self.allow_feedback_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose devices, then press Start.")
        self.level_var = tk.DoubleVar(value=0)

        self._build_ui()
        self.refresh_devices()
        self.after(200, self._poll_router)
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
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"))

        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Refresh Devices", command=lambda: self.refresh_devices(silent=False)).pack(side="right")

        devices = ttk.Frame(root, style="Panel.TFrame", padding=14)
        devices.pack(fill="x", pady=(0, 12))

        self._combo_row(devices, 0, "Windows/app output source", self.source_var, "source_combo")
        self._combo_row(devices, 1, "Output A", self.output_a_var, "output_a_combo")
        self._combo_row(devices, 2, "Output B", self.output_b_var, "output_b_combo")

        volumes = ttk.Frame(root, style="Panel.TFrame", padding=14)
        volumes.pack(fill="x", pady=(0, 12))

        self._volume_row(volumes, 0, "Output A volume", self.volume_a_var)
        self._volume_row(volumes, 1, "Output B volume", self.volume_b_var)

        settings = ttk.Frame(root, style="Panel.TFrame", padding=14)
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="Sample rate", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Combobox(
            settings,
            textvariable=self.sample_rate_var,
            values=("44100", "48000"),
            width=10,
            state="readonly",
        ).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(settings, text="Block size", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(24, 12), pady=4)
        ttk.Combobox(
            settings,
            textvariable=self.block_size_var,
            values=("1024", "2048", "4096", "8192"),
            width=10,
            state="readonly",
        ).grid(row=0, column=3, sticky="w", pady=4)
        ttk.Checkbutton(
            settings,
            text="Allow routing back into the captured output device",
            variable=self.allow_feedback_var,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        note = (
            "For game/system audio, set Windows or the game to play into a virtual audio endpoint, "
            "then choose that endpoint here as the source. This app routes that source to the two "
            "real devices below."
        )
        ttk.Label(settings, text=note, style="Hint.TLabel", wraplength=690).grid(
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
        parent.columnconfigure(1, weight=1)
        setattr(self, attr_name, combo)

    def _volume_row(self, parent: ttk.Frame, row: int, label: str, var: tk.DoubleVar) -> None:
        value = ttk.Label(parent, text=f"{int(var.get())}%", style="Panel.TLabel", width=6)

        def update_value(_event=None) -> None:
            value.configure(text=f"{int(var.get())}%")

        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
        scale = ttk.Scale(parent, variable=var, from_=0, to=500, command=update_value)
        scale.grid(row=row, column=1, sticky="ew", pady=8)
        value.grid(row=row, column=2, sticky="e", padx=(12, 0), pady=8)
        parent.columnconfigure(1, weight=1)

    def refresh_devices(self, silent: bool = False) -> bool:
        try:
            import soundcard as sc
        except Exception as exc:
            self.status_var.set("Install dependencies first: python -m pip install -r requirements.txt")
            if not silent:
                messagebox.showerror(
                    APP_TITLE,
                    "The audio packages are not installed yet.\n\n"
                    "Run run_windows.bat, or run:\n"
                    "python -m pip install -r requirements.txt\n\n"
                    f"Details: {exc}",
                )
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

        new_outputs = [
            DeviceChoice(
                label=f"{speaker.name} ({self._channel_text(speaker.channels)})",
                id=speaker.id,
                name=speaker.name,
                channels=int(speaker.channels),
                kind="speaker",
            )
            for speaker in speakers
        ]

        new_sources = []
        seen_source_ids: set[tuple[str, bool]] = set()
        for microphone in microphones:
            is_loopback = bool(getattr(microphone, "isloopback", False))
            key = (microphone.id, is_loopback)
            if key in seen_source_ids:
                continue
            seen_source_ids.add(key)
            prefix = "Loopback" if is_loopback else "Input"
            new_sources.append(
                DeviceChoice(
                    label=f"{prefix}: {microphone.name} ({self._channel_text(microphone.channels)})",
                    id=microphone.id,
                    name=microphone.name,
                    channels=int(microphone.channels),
                    kind="microphone",
                    is_loopback=is_loopback,
                )
            )

        new_signature = self._device_signature(new_sources, new_outputs)
        changed = new_signature != self.device_signature
        self.sources = new_sources
        self.outputs = new_outputs
        self.device_signature = new_signature

        self.source_combo.configure(values=[device.label for device in self.sources])
        self.output_a_combo.configure(values=[device.label for device in self.outputs])
        self.output_b_combo.configure(values=[device.label for device in self.outputs])

        source_labels = [device.label for device in self.sources]
        output_labels = [device.label for device in self.outputs]

        if old_source and self._contains_device(self.sources, old_source):
            self.source_var.set(self._matching_device(self.sources, old_source).label)
        elif self.source_var.get() not in source_labels:
            preferred_sources = self._preferred_splitter_sources(self.sources)
            loopbacks = [source for source in self.sources if source.is_loopback]
            default_loopbacks = [source for source in loopbacks if source.id == default_speaker.id]
            choices = preferred_sources or default_loopbacks or loopbacks or self.sources
            self.source_var.set(choices[0].label if choices else "")

        if old_output_a and self._contains_device(self.outputs, old_output_a):
            self.output_a_var.set(self._matching_device(self.outputs, old_output_a).label)
        elif self.output_a_var.get() not in output_labels:
            default_outputs = [output for output in self.outputs if output.id == default_speaker.id]
            choices = default_outputs or self.outputs
            self.output_a_var.set(choices[0].label if choices else "")

        if old_output_b and self._contains_device(self.outputs, old_output_b):
            self.output_b_var.set(self._matching_device(self.outputs, old_output_b).label)
        elif self.output_b_var.get() not in output_labels:
            alternate_outputs = [output for output in self.outputs if output.label != self.output_a_var.get()]
            choices = alternate_outputs or self.outputs
            self.output_b_var.set(choices[0].label if choices else "")

        if not self.router or not self.router.is_alive():
            if changed and silent:
                self.status_var.set(
                    f"Device list updated: {len(self.sources)} source(s), {len(self.outputs)} output device(s)."
                )
            else:
                self.status_var.set(f"Found {len(self.sources)} source(s) and {len(self.outputs)} output device(s).")
        return True

    def toggle_router(self) -> None:
        if self.router and self.router.is_alive():
            self._stop_router()
            return

        source = self._selected(self.sources, self.source_var.get())
        output_a = self._selected(self.outputs, self.output_a_var.get())
        output_b = self._selected(self.outputs, self.output_b_var.get())

        if not source or not output_a or not output_b:
            messagebox.showwarning(APP_TITLE, "Choose one capture source and two output devices.")
            return
        if output_a.id == output_b.id:
            messagebox.showwarning(APP_TITLE, "Output A and Output B need to be different devices.")
            return
        if source.is_loopback and source.id in {output_a.id, output_b.id} and not self.allow_feedback_var.get():
            messagebox.showwarning(
                APP_TITLE,
                "One destination is the same device as the loopback capture source.\n\n"
                "That can route the app's own playback back into itself and create echo or feedback. "
                "Use a separate capture endpoint, or enable the checkbox if you really want to test it.",
            )
            return

        try:
            sample_rate = int(self.sample_rate_var.get())
            block_size = int(self.block_size_var.get())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Sample rate and block size must be numbers.")
            return

        self.router = AudioRouter(
            source=source,
            output_a=output_a,
            output_b=output_b,
            sample_rate=sample_rate,
            block_size=block_size,
            volume_a=lambda: self.volume_a_var.get() / 100.0,
            volume_b=lambda: self.volume_b_var.get() / 100.0,
        )
        self.router.set_volumes(self.volume_a_var.get() / 100.0, self.volume_b_var.get() / 100.0)
        self.router.start()
        self.start_button.configure(text="Stop")
        self.status_var.set("Routing audio...")

    def _stop_router(self) -> None:
        if self.router:
            self.router.stop()
        self.router = None
        self.level_var.set(0)
        self.start_button.configure(text="Start")
        self.status_var.set("Stopped.")

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
                self.router.set_volumes(self.volume_a_var.get() / 100.0, self.volume_b_var.get() / 100.0)
                self.level_var.set(self.router.level * 100)
                seconds = self.router.frames_routed / max(1, int(self.sample_rate_var.get()))
                drop_text = f", {self.router.dropped_blocks} dropped block(s)" if self.router.dropped_blocks else ""
                peak_text = ", hot input" if self.router.peak > 0.98 else ""
                self.status_var.set(f"Routing audio... {seconds:,.1f}s processed{drop_text}{peak_text}")
            elif self.router:
                self._stop_router()

        self.after(200, self._poll_router)

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
        if not self._contains_device(self.outputs, self.router.output_a):
            missing.append(self.router.output_a.label)
        if not self._contains_device(self.outputs, self.router.output_b):
            missing.append(self.router.output_b.label)
        return missing

    def _on_close(self) -> None:
        self._stop_router()
        self.destroy()

    @staticmethod
    def _selected(devices: list[DeviceChoice], label: str) -> DeviceChoice | None:
        return next((device for device in devices if device.label == label), None)

    @staticmethod
    def _contains_device(devices: list[DeviceChoice], target: DeviceChoice) -> bool:
        return any(device.id == target.id and device.is_loopback == target.is_loopback for device in devices)

    @staticmethod
    def _matching_device(devices: list[DeviceChoice], target: DeviceChoice) -> DeviceChoice:
        return next(device for device in devices if device.id == target.id and device.is_loopback == target.is_loopback)

    @staticmethod
    def _device_signature(sources: list[DeviceChoice], outputs: list[DeviceChoice]) -> tuple[tuple[str, str, bool], ...]:
        rows = [(device.id, "source", device.is_loopback) for device in sources]
        rows.extend((device.id, "output", device.is_loopback) for device in outputs)
        return tuple(sorted(rows))

    @staticmethod
    def _preferred_splitter_sources(sources: list[DeviceChoice]) -> list[DeviceChoice]:
        exact = [
            source
            for source in sources
            if any(name.lower() == source.name.lower() for name in SPLITTER_SOURCE_NAMES)
        ]
        fuzzy = [
            source
            for source in sources
            if any(name.lower() in source.name.lower() for name in SPLITTER_SOURCE_NAMES)
        ]
        return exact or fuzzy

    @staticmethod
    def _channel_text(channels: int) -> str:
        return "1 channel" if int(channels) == 1 else f"{int(channels)} channels"


if __name__ == "__main__":
    app = DualOutputApp()
    app.mainloop()
