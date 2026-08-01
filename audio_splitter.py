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


@dataclass
class OutputRow:
    frame: ttk.Frame
    label: ttk.Label
    device_var: tk.StringVar
    volume_var: tk.DoubleVar
    value_label: ttk.Label
    combo: ttk.Combobox
    remove_button: ttk.Button


@dataclass(frozen=True)
class OutputRoute:
    device: DeviceChoice
    volume_var: tk.DoubleVar


class AudioRouter:
    def __init__(
        self,
        source: DeviceChoice,
        output_routes: list[OutputRoute],
        sample_rate: int,
        block_size: int,
        master_volume: tk.DoubleVar,
    ) -> None:
        self.source = source
        self.output_routes = output_routes
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.master_volume = master_volume
        self.stop_event = threading.Event()
        self.error_queue: queue.Queue[str] = queue.Queue()
        self.level = 0.0
        self.peak = 0.0
        self.skipped_blocks = 0
        self.frames_routed = 0
        self._thread: threading.Thread | None = None
        self._output_threads: list[threading.Thread] = []
        self._output_queues: list[queue.Queue[object]] = [
            queue.Queue(maxsize=OUTPUT_QUEUE_BLOCKS) for _route in self.output_routes
        ]
        self._volume_lock = threading.Lock()
        self._master_volume = self._read_master_volume(master_volume)
        self._volumes = [self._read_volume(route.volume_var) for route in self.output_routes]

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
            self._master_volume = self._read_master_volume(self.master_volume)
            self._volumes = [self._read_volume(route.volume_var) for route in self.output_routes]

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
        for index, route in enumerate(self.output_routes):
            if route.device.is_none:
                continue
            self._output_threads.append(
                threading.Thread(
                    target=self._play_output,
                    name=f"output-{index + 1}",
                    args=(index, route.device, self._output_queues[index]),
                    daemon=True,
                )
            )
        for thread in self._output_threads:
            thread.start()

    def _play_output(self, output_index: int, device: DeviceChoice, output_queue: queue.Queue[object]) -> None:
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
                    player.play(self._for_output(item, channels, self._volume_for(output_index)))
        except Exception:
            if not self.stop_event.is_set():
                self._report_error(f"Output {output_index + 1} failed:\n{traceback.format_exc()}")
            self.stop_event.set()

    def _enqueue_latest(self, data: object) -> None:
        for route, output_queue in zip(self.output_routes, self._output_queues):
            if not route.device.is_none:
                self._put_latest(output_queue, data)

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
        for output_queue in self._output_queues:
            try:
                output_queue.put_nowait(None)
            except queue.Full:
                try:
                    output_queue.get_nowait()
                    output_queue.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass

    def _volume_for(self, output_index: int) -> float:
        with self._volume_lock:
            if output_index >= len(self._volumes):
                return self._master_volume
            return self._master_volume * self._volumes[output_index]

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
    def _read_master_volume(volume: tk.DoubleVar) -> float:
        return max(0.0, min(1.0, float(volume.get()) / 100.0))

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
        self.geometry("980x700")
        self.minsize(900, 640)

        self.sources: list[DeviceChoice] = []
        self.outputs: list[DeviceChoice] = [self._none_output()]
        self.router: AudioRouter | None = None
        self.device_signature: tuple[tuple[str, str, bool, bool], ...] = ()
        self.live_restart_after_id: str | None = None
        self.output_rows: list[OutputRow] = []

        self.source_var = tk.StringVar()
        self.master_volume_var = tk.DoubleVar(value=100)
        self.sample_rate_var = tk.StringVar(value=str(DEFAULT_SAMPLE_RATE))
        self.block_size_var = tk.StringVar(value=str(DEFAULT_BLOCK_SIZE))
        self.allow_feedback_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a loopback source and at least one additional output.")
        self.level_var = tk.DoubleVar(value=0)
        self.master_volume_var.trace_add("write", lambda *_args: self._apply_live_volumes())

        self._build_ui()
        self.add_output_row(schedule_restart=False)
        self.add_output_row(schedule_restart=False)
        self.refresh_devices()
        self.after(120, self._poll_router)
        self.after(DEVICE_REFRESH_MS, self._poll_devices)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.configure(bg="#edf2f7")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#edf2f7")
        style.configure("Shell.TFrame", background="#edf2f7")
        style.configure("Header.TFrame", background="#111827")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("OutputRow.TFrame", background="#f8fafc", relief="solid", borderwidth=1)
        style.configure("Status.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("TLabel", background="#edf2f7", foreground="#1f2937", font=("Segoe UI", 10))
        style.configure("HeaderTitle.TLabel", background="#111827", foreground="#f9fafb", font=("Segoe UI", 21, "bold"))
        style.configure("HeaderSub.TLabel", background="#111827", foreground="#cbd5e1", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#111827", font=("Segoe UI", 12, "bold"))
        style.configure("CardSub.TLabel", background="#ffffff", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("Field.TLabel", background="#ffffff", foreground="#334155", font=("Segoe UI", 9, "bold"))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("Row.TLabel", background="#f8fafc", foreground="#334155", font=("Segoe UI", 10, "bold"))
        style.configure("RowValue.TLabel", background="#f8fafc", foreground="#0f172a", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", background="#ffffff", foreground="#475569", font=("Segoe UI", 10))
        style.configure("Primary.TButton", font=("Segoe UI", 11, "bold"), padding=(16, 8))
        style.configure("Action.TButton", font=("Segoe UI", 9, "bold"), padding=(12, 7))
        style.configure("Quiet.TButton", font=("Segoe UI", 9), padding=(10, 6))
        style.configure("Danger.TButton", font=("Segoe UI", 9), padding=(10, 5))
        style.map("Primary.TButton", foreground=[("active", "#ffffff"), ("!disabled", "#ffffff")], background=[("active", "#2563eb"), ("!disabled", "#1d4ed8")])
        style.map("Action.TButton", foreground=[("active", "#ffffff"), ("!disabled", "#ffffff")], background=[("active", "#0f766e"), ("!disabled", "#0d9488")])
        style.map("Danger.TButton", foreground=[("active", "#ffffff"), ("!disabled", "#ffffff")], background=[("active", "#b91c1c"), ("!disabled", "#dc2626")])
        style.configure("Level.Horizontal.TProgressbar", troughcolor="#e2e8f0", background="#22c55e", bordercolor="#e2e8f0", lightcolor="#22c55e", darkcolor="#16a34a")

        shell = ttk.Frame(self, style="Shell.TFrame")
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell, style="Header.TFrame", padding=(22, 18))
        header.pack(fill="x")
        title_block = ttk.Frame(header, style="Header.TFrame")
        title_block.pack(side="left", fill="x", expand=True)
        ttk.Label(title_block, text=APP_TITLE, style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            title_block,
            text="Mirror one live Windows audio stream to multiple outputs with independent volume.",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Button(header, text="Refresh Devices", style="Quiet.TButton", command=lambda: self.refresh_devices(silent=False)).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Install Optional Driver", style="Quiet.TButton", command=self._install_optional_driver).pack(side="right")

        root = ttk.Frame(shell, style="Shell.TFrame", padding=18)
        root.pack(fill="both", expand=True)

        devices, devices_content = self._section(
            root,
            "Capture",
            "Choose the live loopback stream that Audio Splitter should mirror.",
        )
        devices.pack(fill="x", pady=(0, 12))
        self._combo_row(devices_content, 0, "Loopback source", self.source_var, "source_combo")

        outputs, outputs_content = self._section(
            root,
            "Additional Outputs",
            "Add one row per playback device. Each row has its own live volume.",
        )
        outputs.pack(fill="both", expand=True, pady=(0, 12))
        outputs_header = ttk.Frame(outputs_content, style="Card.TFrame")
        outputs_header.pack(fill="x", pady=(0, 10))
        ttk.Button(outputs_header, text="Add Output", style="Action.TButton", command=self.add_output_row).pack(side="right")

        outputs_body = ttk.Frame(outputs_content, style="Card.TFrame")
        outputs_body.pack(fill="both", expand=True)
        self.outputs_canvas = tk.Canvas(outputs_body, bg="#ffffff", highlightthickness=0, height=210)
        outputs_scrollbar = ttk.Scrollbar(outputs_body, orient="vertical", command=self.outputs_canvas.yview)
        self.outputs_frame = ttk.Frame(self.outputs_canvas, style="Card.TFrame")
        self.outputs_frame_id = self.outputs_canvas.create_window((0, 0), window=self.outputs_frame, anchor="nw")
        self.outputs_canvas.configure(yscrollcommand=outputs_scrollbar.set)
        self.outputs_canvas.pack(side="left", fill="both", expand=True)
        outputs_scrollbar.pack(side="right", fill="y")
        self.outputs_frame.bind(
            "<Configure>",
            lambda _event: self.outputs_canvas.configure(scrollregion=self.outputs_canvas.bbox("all")),
        )
        self.outputs_canvas.bind(
            "<Configure>",
            lambda event: self.outputs_canvas.itemconfigure(self.outputs_frame_id, width=event.width),
        )
        self.outputs_canvas.bind_all("<MouseWheel>", self._on_outputs_mousewheel)

        settings, settings_content = self._section(
            root,
            "Routing Settings",
            "Latency and guardrails for the live audio stream.",
        )
        settings.pack(fill="x", pady=(0, 12))
        self._master_volume_row(settings_content)

        ttk.Label(settings_content, text="Sample rate", style="Field.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.sample_rate_combo = ttk.Combobox(
            settings_content,
            textvariable=self.sample_rate_var,
            values=("44100", "48000"),
            width=10,
            state="readonly",
        )
        self.sample_rate_combo.grid(row=1, column=1, sticky="w", pady=6)
        self.sample_rate_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart("sample rate"))

        ttk.Label(settings_content, text="Block size", style="Field.TLabel").grid(row=1, column=2, sticky="w", padx=(28, 12), pady=6)
        self.block_size_combo = ttk.Combobox(
            settings_content,
            textvariable=self.block_size_var,
            values=("128", "256", "512", "1024", "2048", "4096"),
            width=10,
            state="readonly",
        )
        self.block_size_combo.grid(row=1, column=3, sticky="w", pady=6)
        self.block_size_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart("block size"))

        ttk.Checkbutton(
            settings_content,
            text="Allow output back into the captured source device",
            variable=self.allow_feedback_var,
            command=lambda: self._schedule_live_restart("feedback setting"),
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        note = (
            "For the cleanest no-driver fallback, set Windows to play through one device you can already hear, "
            "choose that device's Loopback source here, then route only to the other device. "
            "Do not also route back into the captured source unless you need to test it."
        )
        ttk.Label(settings_content, text=note, style="Hint.TLabel", wraplength=720).grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0),
        )

        bottom = ttk.Frame(root, style="Status.TFrame", padding=(14, 12))
        bottom.pack(fill="x")
        self.start_button = ttk.Button(bottom, text="Start", style="Primary.TButton", command=self.toggle_router)
        self.start_button.pack(side="left")
        ttk.Progressbar(
            bottom,
            variable=self.level_var,
            maximum=100,
            length=210,
            style="Level.Horizontal.TProgressbar",
        ).pack(side="left", padx=(14, 12))
        ttk.Label(bottom, textvariable=self.status_var, style="Status.TLabel").pack(side="left", fill="x", expand=True)

    def _section(self, parent: ttk.Frame, title: str, subtitle: str) -> tuple[ttk.Frame, ttk.Frame]:
        section = ttk.Frame(parent, style="Card.TFrame", padding=16)
        ttk.Label(section, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(section, text=subtitle, style="CardSub.TLabel").pack(anchor="w", pady=(2, 12))
        content = ttk.Frame(section, style="Card.TFrame")
        content.pack(fill="both", expand=True)
        return section, content

    def _combo_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, attr_name: str) -> None:
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
        combo = ttk.Combobox(parent, textvariable=var, state="readonly", width=82)
        combo.grid(row=row, column=1, sticky="ew", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart(label.lower()))
        parent.columnconfigure(1, weight=1)
        setattr(self, attr_name, combo)

    def _master_volume_row(self, parent: ttk.Frame) -> None:
        value_label = ttk.Label(parent, text=f"{int(self.master_volume_var.get())}%", style="Field.TLabel", width=6)

        def update_value(_event=None) -> None:
            value_label.configure(text=f"{int(self.master_volume_var.get())}%")
            self._apply_live_volumes()

        ttk.Label(parent, text="Main output volume", style="Field.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=(0, 10),
        )
        scale = ttk.Scale(parent, variable=self.master_volume_var, from_=0, to=100, command=update_value)
        scale.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 10))
        value_label.grid(row=0, column=3, sticky="e", padx=(12, 0), pady=(0, 10))
        parent.columnconfigure(1, weight=1)

    def add_output_row(self, schedule_restart: bool = True) -> None:
        row_number = len(self.output_rows) + 1
        device_var = tk.StringVar(value=NONE_LABEL)
        volume_var = tk.DoubleVar(value=80)
        frame = ttk.Frame(self.outputs_frame, style="OutputRow.TFrame", padding=(12, 10))
        frame.pack(fill="x", padx=(0, 8), pady=(0, 10))

        label = ttk.Label(frame, text=f"Output {row_number}", style="Row.TLabel", width=10)
        label.grid(
            row=0,
            column=0,
            rowspan=2,
            sticky="w",
            padx=(0, 12),
        )
        combo = ttk.Combobox(
            frame,
            textvariable=device_var,
            state="readonly",
            width=58,
            values=[device.label for device in self.outputs],
        )
        combo.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_live_restart("output selection"))

        value_label = ttk.Label(frame, text=f"{int(volume_var.get())}%", style="RowValue.TLabel", width=6)

        def update_value(_event=None) -> None:
            value_label.configure(text=f"{int(volume_var.get())}%")
            self._apply_live_volumes()

        volume_var.trace_add("write", lambda *_args: self._apply_live_volumes())
        scale = ttk.Scale(frame, variable=volume_var, from_=0, to=500, command=update_value)
        scale.grid(row=1, column=1, sticky="ew")
        value_label.grid(row=1, column=2, sticky="e", padx=(12, 8))
        remove_button = ttk.Button(frame, text="Remove", style="Danger.TButton", command=lambda: self.remove_output_row(frame))
        remove_button.grid(row=0, column=2, sticky="e", padx=(12, 8))

        frame.columnconfigure(1, weight=1)
        self.output_rows.append(
            OutputRow(
                frame=frame,
                label=label,
                device_var=device_var,
                volume_var=volume_var,
                value_label=value_label,
                combo=combo,
                remove_button=remove_button,
            )
        )
        self._renumber_output_rows()
        if schedule_restart:
            self._schedule_live_restart("output list")

    def remove_output_row(self, frame: ttk.Frame) -> None:
        if len(self.output_rows) <= 1:
            return
        for index, row in enumerate(self.output_rows):
            if row.frame == frame:
                row.frame.destroy()
                del self.output_rows[index]
                break
        self._renumber_output_rows()
        self._schedule_live_restart("output list")

    def _renumber_output_rows(self) -> None:
        for index, row in enumerate(self.output_rows, start=1):
            row.label.configure(text=f"Output {index}")
            row.remove_button.configure(state="disabled" if len(self.output_rows) <= 1 else "normal")

    def _on_outputs_mousewheel(self, event: tk.Event) -> None:
        if not hasattr(self, "outputs_canvas"):
            return
        self.outputs_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

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
        old_output_rows = [
            self._selected(self.outputs, row.device_var.get())
            for row in self.output_rows
        ]

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
        for row in self.output_rows:
            row.combo.configure(values=[device.label for device in self.outputs])

        self._restore_or_default(old_source, self.sources, self.source_var, self._default_source_id(default_speaker.id))
        for old_output, row in zip(old_output_rows, self.output_rows):
            self._restore_or_default(old_output, self.outputs, row.device_var, None)

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
        selected_rows = [
            (row, self._selected(self.outputs, row.device_var.get()))
            for row in self.output_rows
        ]
        if not source or any(output is None for _row, output in selected_rows):
            messagebox.showwarning(APP_TITLE, "Choose a source and output choices.")
            return False
        routes = [
            OutputRoute(device=output, volume_var=row.volume_var)
            for row, output in selected_rows
            if output and not output.is_none
        ]
        if not routes:
            messagebox.showwarning(APP_TITLE, "Choose at least one additional output.")
            return False

        duplicate_names = self._duplicate_output_names([route.device for route in routes])
        if duplicate_names:
            messagebox.showwarning(
                APP_TITLE,
                "Each additional output should be selected only once:\n\n"
                + "\n".join(f"- {name}" for name in duplicate_names),
            )
            return False

        if not self.allow_feedback_var.get():
            for route in routes:
                if route.device.id == source.id:
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
            output_routes=routes,
            sample_rate=sample_rate,
            block_size=block_size,
            master_volume=self.master_volume_var,
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
        for route in self.router.output_routes:
            if not route.device.is_none and not self._contains_device(self.outputs, route.device):
                missing.append(route.device.label)
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
    def _duplicate_output_names(devices: list[DeviceChoice]) -> list[str]:
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for device in devices:
            if device.id in seen:
                duplicates.append(seen[device.id])
            else:
                seen[device.id] = device.label
        return duplicates

    @staticmethod
    def _device_signature(sources: list[DeviceChoice], outputs: list[DeviceChoice]) -> tuple[tuple[str, str, bool, bool], ...]:
        rows = [(device.id, device.label, device.is_loopback, device.is_none) for device in sources + outputs]
        return tuple(sorted(rows))

    @staticmethod
    def _channel_text(channels: int) -> str:
        return "1 channel" if int(channels) == 1 else f"{int(channels)} channels"


if __name__ == "__main__":
    AudioSplitterApp().mainloop()
