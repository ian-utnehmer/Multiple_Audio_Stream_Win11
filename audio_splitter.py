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
from tkinter import messagebox

import customtkinter as ctk


APP_TITLE = "Audio Splitter"
APP_USER_MODEL_ID = "AudioSplitter.App"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_BLOCK_SIZE = 512
DEVICE_REFRESH_MS = 1500
LIVE_RESTART_MS = 75
APP_OUTPUT_QUEUE_BLOCKS = 1
STARTUP_DISCARD_SECONDS = 0.08
MAX_VOLUME = 5.0
ERROR_LOG = Path(__file__).with_name("audio_splitter_error.log")
ICON_PATH = Path(__file__).with_name("assets") / "audio_splitter.ico"
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
    frame: ctk.CTkFrame
    label: ctk.CTkLabel
    device_var: tk.StringVar
    volume_var: tk.DoubleVar
    value_label: ctk.CTkLabel
    combo: ctk.CTkOptionMenu
    remove_button: ctk.CTkButton


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
        self.output_queue_blocks = APP_OUTPUT_QUEUE_BLOCKS
        self.output_handoff_timeout = self._handoff_timeout(sample_rate, block_size)
        self._output_queues: list[queue.Queue[object]] = [
            queue.Queue(maxsize=self.output_queue_blocks) for _route in self.output_routes
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

            self._configure_realtime_thread()
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
                    data = recorder.record(numframes=self.block_size)
                    if data.size == 0:
                        time.sleep(0.001)
                        continue

                    self.level = float(min(1.0, math.sqrt(float(np.mean(np.square(data)))) * 4.0))
                    self.peak = float(min(1.0, np.max(np.abs(data))))
                    self._enqueue_audio(data)
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

            self._configure_realtime_thread()
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

                    player.play(self._for_output(item, channels, self._volume_for(output_index)))
        except Exception:
            if not self.stop_event.is_set():
                self._report_error(f"Output {output_index + 1} failed:\n{traceback.format_exc()}")
            self.stop_event.set()

    def _enqueue_audio(self, data: object) -> None:
        for route, output_queue in zip(self.output_routes, self._output_queues):
            if not route.device.is_none:
                self._put_realtime(output_queue, data)

    def _put_realtime(self, output_queue: queue.Queue[object], data: object) -> None:
        try:
            output_queue.put(data, timeout=self.output_handoff_timeout)
            return
        except queue.Full:
            pass

        if not self._drop_oldest_pending(output_queue):
            return
        try:
            output_queue.put_nowait(data)
        except queue.Full:
            self.skipped_blocks += 1

    def _drop_oldest_pending(self, output_queue: queue.Queue[object]) -> bool:
        try:
            pending = output_queue.get_nowait()
        except queue.Empty:
            return True
        if pending is None:
            self.stop_event.set()
            try:
                output_queue.put_nowait(None)
            except queue.Full:
                pass
            return False
        self.skipped_blocks += 1
        return True

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
                recorder.record(numframes=self.block_size)
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
    def _configure_realtime_thread() -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            winmm = ctypes.WinDLL("winmm", use_last_error=True)
            winmm.timeBeginPeriod(1)
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)
        except Exception:
            pass

    @staticmethod
    def _usable_channels(channels: int) -> int:
        try:
            count = int(channels)
        except Exception:
            return 2
        return 1 if count <= 1 else 2

    @staticmethod
    def _handoff_timeout(sample_rate: int, block_size: int) -> float:
        if sample_rate <= 0 or block_size <= 0:
            return 0.005
        block_seconds = block_size / sample_rate
        return max(0.002, min(0.012, block_seconds))

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


class AudioSplitterApp(ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x740")
        self.minsize(940, 660)
        self._set_window_icon()

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

    def _set_window_icon(self) -> None:
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
            except Exception:
                pass

        if ICON_PATH.exists():
            try:
                self.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass

    def _build_ui(self) -> None:
        self.configure(fg_color="#f8fafc")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=284, corner_radius=0, fg_color="#0f172a")
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(5, weight=1)

        title = ctk.CTkLabel(
            sidebar,
            text=APP_TITLE,
            text_color="#f8fafc",
            font=ctk.CTkFont(family="Segoe UI", size=25, weight="bold"),
            anchor="w",
        )
        title.grid(row=0, column=0, sticky="ew", padx=22, pady=(28, 4))
        ctk.CTkLabel(
            sidebar,
            text="Live Windows audio routed to every output you choose.",
            text_color="#cbd5e1",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            justify="left",
            wraplength=224,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 26))

        self.start_button = ctk.CTkButton(
            sidebar,
            text="Start Routing",
            command=self.toggle_router,
            height=44,
            corner_radius=10,
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            text_color="#ffffff",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
        )
        self.start_button.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 12))
        ctk.CTkButton(
            sidebar,
            text="Refresh Devices",
            command=lambda: self.refresh_devices(silent=False),
            height=38,
            corner_radius=10,
            fg_color="#1e293b",
            hover_color="#334155",
            text_color="#e2e8f0",
        ).grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 10))
        ctk.CTkButton(
            sidebar,
            text="Install Optional Driver",
            command=self._install_optional_driver,
            height=38,
            corner_radius=10,
            fg_color="#1e293b",
            hover_color="#334155",
            text_color="#e2e8f0",
        ).grid(row=4, column=0, sticky="ew", padx=22, pady=(0, 10))

        status_panel = ctk.CTkFrame(sidebar, fg_color="#111c2d", corner_radius=14)
        status_panel.grid(row=6, column=0, sticky="ew", padx=18, pady=(16, 22))
        ctk.CTkLabel(
            status_panel,
            text="Signal",
            text_color="#94a3b8",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 6))
        self.level_bar = ctk.CTkProgressBar(
            status_panel,
            height=10,
            corner_radius=10,
            fg_color="#1e293b",
            progress_color="#22c55e",
        )
        self.level_bar.pack(fill="x", padx=14)
        self.level_bar.set(0)
        ctk.CTkLabel(
            status_panel,
            textvariable=self.status_var,
            text_color="#dbeafe",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            justify="left",
            wraplength=220,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 14))

        root = ctk.CTkScrollableFrame(
            self,
            corner_radius=0,
            fg_color="#f8fafc",
            scrollbar_fg_color="#e2e8f0",
            scrollbar_button_color="#64748b",
            scrollbar_button_hover_color="#475569",
        )
        root.grid(row=0, column=1, sticky="nsew")
        root.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(26, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="Routing Console",
            text_color="#0f172a",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            header,
            text="Pick a loopback source, add output devices, and tune every stream in real time.",
            text_color="#64748b",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        devices, devices_content = self._section(
            root,
            1,
            "Capture",
            "Choose the live loopback stream that Audio Splitter should mirror.",
        )
        self._option_row(devices_content, 0, "Loopback source", self.source_var, "source_combo")

        outputs, outputs_content = self._section(
            root,
            2,
            "Additional Outputs",
            "Add one row per playback device. Each row has its own live volume.",
        )
        outputs_content.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            outputs,
            text="Add Output",
            command=self.add_output_row,
            width=128,
            height=36,
            corner_radius=9,
            fg_color="#0f766e",
            hover_color="#115e59",
            text_color="#ffffff",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        ).grid(row=0, column=1, sticky="e", padx=(12, 28), pady=(22, 0))
        self.outputs_frame = ctk.CTkScrollableFrame(
            outputs_content,
            height=286,
            corner_radius=12,
            fg_color="transparent",
            scrollbar_fg_color="#e2e8f0",
            scrollbar_button_color="#64748b",
            scrollbar_button_hover_color="#475569",
        )
        self.outputs_frame.grid(row=0, column=0, sticky="nsew")

        settings, settings_content = self._section(
            root,
            3,
            "Routing Settings",
            "Latency and guardrails for the live audio stream.",
        )
        self._master_volume_row(settings_content)

        ctk.CTkLabel(
            settings_content,
            text="Sample rate",
            text_color="#334155",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=(0, 14), pady=(12, 8))
        self.sample_rate_combo = ctk.CTkSegmentedButton(
            settings_content,
            variable=self.sample_rate_var,
            values=["44100", "48000"],
            command=lambda _value: self._schedule_live_restart("sample rate"),
            height=34,
            corner_radius=9,
            fg_color="#e2e8f0",
            selected_color="#2563eb",
            selected_hover_color="#1d4ed8",
            unselected_color="#e2e8f0",
            unselected_hover_color="#cbd5e1",
        )
        self.sample_rate_combo.grid(row=1, column=1, sticky="w", pady=(12, 8))

        ctk.CTkLabel(
            settings_content,
            text="Block size",
            text_color="#334155",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=(0, 14), pady=8)
        self.block_size_combo = ctk.CTkSegmentedButton(
            settings_content,
            variable=self.block_size_var,
            values=["64", "128", "256", "512", "1024", "2048", "4096"],
            command=lambda _value: self._schedule_live_restart("block size"),
            height=34,
            corner_radius=9,
            fg_color="#e2e8f0",
            selected_color="#2563eb",
            selected_hover_color="#1d4ed8",
            unselected_color="#e2e8f0",
            unselected_hover_color="#cbd5e1",
        )
        self.block_size_combo.grid(row=2, column=1, sticky="ew", pady=8)

        ctk.CTkCheckBox(
            settings_content,
            text="Allow output back into the captured source device",
            variable=self.allow_feedback_var,
            command=lambda: self._schedule_live_restart("feedback setting"),
            fg_color="#2563eb",
            hover_color="#1d4ed8",
            border_color="#94a3b8",
            text_color="#334155",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 4))

        note = (
            "For the cleanest no-driver fallback, set Windows to play through one device you can already hear, "
            "choose that device's loopback source here, then route only to the other device. "
            "Do not also route back into the captured source unless you need to test it."
        )
        ctk.CTkLabel(
            settings_content,
            text=note,
            text_color="#64748b",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            justify="left",
            wraplength=720,
            anchor="w",
        ).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _section(
        self,
        parent: ctk.CTkScrollableFrame,
        row: int,
        title: str,
        subtitle: str,
    ) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        section = ctk.CTkFrame(parent, fg_color="#ffffff", border_color="#e2e8f0", border_width=1, corner_radius=16)
        section.grid(row=row, column=0, sticky="ew", padx=28, pady=(0, 16))
        section.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            section,
            text=title,
            text_color="#0f172a",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 2))
        ctk.CTkLabel(
            section,
            text=subtitle,
            text_color="#64748b",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))
        content = ctk.CTkFrame(section, fg_color="transparent")
        content.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=18, pady=(0, 18))
        content.grid_columnconfigure(1, weight=1)
        return section, content

    def _option_row(
        self,
        parent: ctk.CTkFrame,
        row: int,
        label: str,
        var: tk.StringVar,
        attr_name: str,
    ) -> None:
        ctk.CTkLabel(
            parent,
            text=label,
            text_color="#334155",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
        combo = ctk.CTkOptionMenu(
            parent,
            variable=var,
            values=["Loading devices..."],
            command=lambda _value: self._schedule_live_restart(label.lower()),
            height=36,
            corner_radius=9,
            fg_color="#f8fafc",
            button_color="#2563eb",
            button_hover_color="#1d4ed8",
            dropdown_fg_color="#ffffff",
            dropdown_hover_color="#dbeafe",
            dropdown_text_color="#0f172a",
            text_color="#0f172a",
        )
        combo.grid(row=row, column=1, sticky="ew", pady=6)
        setattr(self, attr_name, combo)

    def _master_volume_row(self, parent: ctk.CTkFrame) -> None:
        value_label = ctk.CTkLabel(
            parent,
            text=f"{int(self.master_volume_var.get())}%",
            text_color="#0f172a",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            width=52,
            anchor="e",
        )

        def update_value(_value=None) -> None:
            value_label.configure(text=f"{int(self.master_volume_var.get())}%")
            self._apply_live_volumes()

        ctk.CTkLabel(
            parent,
            text="Main output volume",
            text_color="#334155",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 14), pady=(0, 12))
        ctk.CTkSlider(
            parent,
            variable=self.master_volume_var,
            from_=0,
            to=100,
            number_of_steps=100,
            command=update_value,
            height=18,
            button_color="#2563eb",
            button_hover_color="#1d4ed8",
            progress_color="#2563eb",
            fg_color="#e2e8f0",
        ).grid(row=0, column=1, sticky="ew", pady=(0, 12))
        value_label.grid(row=0, column=2, sticky="e", padx=(12, 0), pady=(0, 12))
        parent.columnconfigure(1, weight=1)

    def _set_level(self, value: float) -> None:
        clamped = max(0.0, min(1.0, float(value)))
        self.level_var.set(clamped * 100)
        if hasattr(self, "level_bar"):
            self.level_bar.set(clamped)

    def add_output_row(self, schedule_restart: bool = True) -> None:
        row_number = len(self.output_rows) + 1
        device_var = tk.StringVar(value=NONE_LABEL)
        volume_var = tk.DoubleVar(value=80)
        frame = ctk.CTkFrame(
            self.outputs_frame,
            fg_color="#f8fafc",
            border_color="#e2e8f0",
            border_width=1,
            corner_radius=12,
        )
        frame.pack(fill="x", padx=(0, 8), pady=(0, 10))
        frame.grid_columnconfigure(1, weight=1)

        label = ctk.CTkLabel(
            frame,
            text=f"Output {row_number}",
            text_color="#0f172a",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            anchor="w",
            width=86,
        )
        label.grid(
            row=0,
            column=0,
            rowspan=2,
            sticky="w",
            padx=(14, 12),
            pady=12,
        )
        combo = ctk.CTkOptionMenu(
            frame,
            variable=device_var,
            values=[device.label for device in self.outputs] or [NONE_LABEL],
            command=lambda _value: self._schedule_live_restart("output selection"),
            height=34,
            corner_radius=9,
            fg_color="#ffffff",
            button_color="#2563eb",
            button_hover_color="#1d4ed8",
            dropdown_fg_color="#ffffff",
            dropdown_hover_color="#dbeafe",
            dropdown_text_color="#0f172a",
            text_color="#0f172a",
        )
        combo.grid(row=0, column=1, sticky="ew", pady=(12, 7))

        value_label = ctk.CTkLabel(
            frame,
            text=f"{int(volume_var.get())}%",
            text_color="#0f172a",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            width=54,
            anchor="e",
        )

        def update_value(_value=None) -> None:
            value_label.configure(text=f"{int(volume_var.get())}%")
            self._apply_live_volumes()

        volume_var.trace_add("write", lambda *_args: self._apply_live_volumes())
        ctk.CTkSlider(
            frame,
            variable=volume_var,
            from_=0,
            to=500,
            number_of_steps=500,
            command=update_value,
            height=18,
            button_color="#0f766e",
            button_hover_color="#115e59",
            progress_color="#0f766e",
            fg_color="#dbeafe",
        ).grid(row=1, column=1, sticky="ew", pady=(0, 12))
        value_label.grid(row=1, column=2, sticky="e", padx=(12, 12), pady=(0, 12))
        remove_button = ctk.CTkButton(
            frame,
            text="Remove",
            command=lambda: self.remove_output_row(frame),
            width=86,
            height=32,
            corner_radius=9,
            fg_color="#ef4444",
            hover_color="#dc2626",
            text_color="#ffffff",
        )
        remove_button.grid(row=0, column=2, sticky="e", padx=(12, 12), pady=(12, 7))
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

    def remove_output_row(self, frame: ctk.CTkFrame) -> None:
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

        new_sources: list[DeviceChoice] = []
        seen_sources: set[tuple[str, bool]] = set()
        for microphone in microphones:
            is_loopback = bool(getattr(microphone, "isloopback", False))
            if not is_loopback:
                continue
            key = (microphone.id, is_loopback)
            if key in seen_sources:
                continue
            seen_sources.add(key)
            new_sources.append(
                DeviceChoice(
                    label=f"Loopback: {microphone.name} ({self._channel_text(microphone.channels)})",
                    id=microphone.id,
                    name=microphone.name,
                    channels=int(microphone.channels),
                    is_loopback=True,
                )
            )

        new_outputs = [self._none_output()]
        new_outputs.extend(
            DeviceChoice(
                label=f"{speaker.name} ({self._channel_text(speaker.channels)})",
                id=speaker.id,
                name=speaker.name,
                channels=int(speaker.channels),
            )
            for speaker in speakers
        )

        signature = self._device_signature(new_sources, new_outputs)
        changed = signature != self.device_signature
        if not changed and silent:
            return True

        self.sources = new_sources
        self.outputs = new_outputs
        self.device_signature = signature

        source_values = [device.label for device in self.sources] or ["No loopback sources found"]
        self.source_combo.configure(values=source_values)
        for row in self.output_rows:
            row.combo.configure(values=[device.label for device in self.outputs] or [NONE_LABEL])

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
        self.start_button.configure(text="Stop Routing", fg_color="#dc2626", hover_color="#b91c1c")
        self.status_var.set("Routing current audio...")
        return True

    def _stop_router(self, status: str = "Stopped.") -> None:
        if self.live_restart_after_id:
            self.after_cancel(self.live_restart_after_id)
            self.live_restart_after_id = None
        if self.router:
            self.router.stop()
        self.router = None
        self._set_level(0)
        self.start_button.configure(text="Start Routing", fg_color="#2563eb", hover_color="#1d4ed8")
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
                self._set_level(self.router.level)
                seconds = self.router.frames_routed / max(1, int(self.sample_rate_var.get()))
                skip_text = f", {self.router.skipped_blocks} resync block(s)" if self.router.skipped_blocks else ""
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
