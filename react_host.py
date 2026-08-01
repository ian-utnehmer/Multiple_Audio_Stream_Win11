from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from audio_splitter import (
    APP_TITLE,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_SAMPLE_RATE,
    ERROR_LOG,
    LIVE_RESTART_MS,
    NONE_LABEL,
    AudioRouter,
    DeviceChoice,
    OutputRoute,
)


ROOT = Path(__file__).resolve().parent
WEB_DIST = ROOT / "web" / "dist"
SHUTDOWN_DELAY_SECONDS = 0.15
NONE_KEY = "__none__"


class FloatSetting:
    def __init__(self, value: float) -> None:
        self._value = float(value)
        self._lock = threading.Lock()

    def get(self) -> float:
        with self._lock:
            return self._value

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)


@dataclass
class OutputSelection:
    id: str
    device_key: str
    volume: FloatSetting


class SplitterControl:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sources: list[DeviceChoice] = []
        self.outputs: list[DeviceChoice] = [self._none_output()]
        self.device_signature: tuple[tuple[str, str, bool, bool], ...] = ()
        self.source_key = ""
        self.output_rows = [
            OutputSelection(id=self._new_row_id(), device_key=NONE_KEY, volume=FloatSetting(80)),
            OutputSelection(id=self._new_row_id(), device_key=NONE_KEY, volume=FloatSetting(80)),
        ]
        self.master_volume = FloatSetting(100)
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.block_size = DEFAULT_BLOCK_SIZE
        self.allow_feedback = False
        self.router: AudioRouter | None = None
        self.status = "Choose a loopback source and at least one additional output."
        self.last_error = ""
        self.live_restart_timer: threading.Timer | None = None
        self.refresh_devices(silent=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.refresh_devices(silent=True)
            self._poll_router_errors()
            running = bool(self.router and self.router.is_alive())
            frames = self.router.frames_routed if self.router else 0
            seconds = frames / max(1, self.sample_rate)
            return {
                "appTitle": APP_TITLE,
                "devices": {
                    "sources": [self._serialize_device(device) for device in self.sources],
                    "outputs": [self._serialize_device(device) for device in self.outputs],
                },
                "selection": {
                    "sourceKey": self.source_key,
                    "outputs": [
                        {
                            "id": row.id,
                            "deviceKey": row.device_key,
                            "volume": row.volume.get(),
                        }
                        for row in self.output_rows
                    ],
                },
                "settings": {
                    "masterVolume": self.master_volume.get(),
                    "sampleRate": self.sample_rate,
                    "blockSize": self.block_size,
                    "allowFeedback": self.allow_feedback,
                },
                "routing": {
                    "running": running,
                    "level": self.router.level if running and self.router else 0,
                    "peak": self.router.peak if running and self.router else 0,
                    "seconds": seconds if running else 0,
                    "skippedBlocks": self.router.skipped_blocks if running and self.router else 0,
                    "resyncBlocks": self.router.skipped_blocks if running and self.router else 0,
                    "queueBlocks": self.router.output_queue_blocks if self.router else 0,
                },
                "status": self.status,
                "lastError": self.last_error,
            }

    def refresh_devices(self, silent: bool = False) -> dict[str, Any]:
        with self.lock:
            try:
                import soundcard as sc

                speakers = sc.all_speakers()
                default_speaker = sc.default_speaker()
                microphones = sc.all_microphones(include_loopback=True)
            except Exception as exc:
                self.status = "Could not read Windows audio devices."
                self.last_error = str(exc)
                return self.snapshot_without_refresh()

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

            if self.source_key and not self._device_by_key(self.sources, self.source_key):
                self.source_key = ""
            if not self.source_key:
                self.source_key = self._default_source_key(getattr(default_speaker, "id", ""))

            output_keys = {self._device_key(device) for device in self.outputs}
            for row in self.output_rows:
                if row.device_key not in output_keys:
                    row.device_key = NONE_KEY

            missing = self._missing_active_devices()
            if missing:
                self.stop(f"Routing stopped because a selected device disappeared: {', '.join(missing)}")
            elif changed and silent and not self._is_running():
                self.status = f"Device list updated: {len(self.sources)} loopback source(s), {len(self.outputs) - 1} output(s)."
            elif not self._is_running():
                self.status = f"Found {len(self.sources)} loopback source(s) and {len(self.outputs) - 1} output device(s)."
            return self.snapshot_without_refresh()

    def snapshot_without_refresh(self) -> dict[str, Any]:
        running = bool(self.router and self.router.is_alive())
        frames = self.router.frames_routed if self.router else 0
        seconds = frames / max(1, self.sample_rate)
        return {
            "appTitle": APP_TITLE,
            "devices": {
                "sources": [self._serialize_device(device) for device in self.sources],
                "outputs": [self._serialize_device(device) for device in self.outputs],
            },
            "selection": {
                "sourceKey": self.source_key,
                "outputs": [
                    {"id": row.id, "deviceKey": row.device_key, "volume": row.volume.get()}
                    for row in self.output_rows
                ],
            },
            "settings": {
                "masterVolume": self.master_volume.get(),
                "sampleRate": self.sample_rate,
                "blockSize": self.block_size,
                "allowFeedback": self.allow_feedback,
            },
            "routing": {
                "running": running,
                "level": self.router.level if running and self.router else 0,
                "peak": self.router.peak if running and self.router else 0,
                "seconds": seconds if running else 0,
                "skippedBlocks": self.router.skipped_blocks if running and self.router else 0,
                "resyncBlocks": self.router.skipped_blocks if running and self.router else 0,
                "queueBlocks": self.router.output_queue_blocks if self.router else 0,
            },
            "status": self.status,
            "lastError": self.last_error,
        }

    def add_output(self) -> dict[str, Any]:
        with self.lock:
            self.output_rows.append(OutputSelection(id=self._new_row_id(), device_key=NONE_KEY, volume=FloatSetting(80)))
            self._schedule_restart_if_running("output list")
            return self.snapshot_without_refresh()

    def remove_output(self, row_id: str) -> dict[str, Any]:
        with self.lock:
            if len(self.output_rows) <= 1:
                return self.snapshot_without_refresh()
            self.output_rows = [row for row in self.output_rows if row.id != row_id]
            if not self.output_rows:
                self.output_rows.append(OutputSelection(id=self._new_row_id(), device_key=NONE_KEY, volume=FloatSetting(80)))
            self._schedule_restart_if_running("output list")
            return self.snapshot_without_refresh()

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            restart_needed = False
            volume_changed = False

            if "sourceKey" in payload and payload["sourceKey"] != self.source_key:
                self.source_key = str(payload["sourceKey"])
                restart_needed = True

            if "masterVolume" in payload:
                self.master_volume.set(self._clamp(float(payload["masterVolume"]), 0, 100))
                volume_changed = True

            if "sampleRate" in payload:
                sample_rate = int(payload["sampleRate"])
                if sample_rate != self.sample_rate:
                    self.sample_rate = sample_rate
                    restart_needed = True

            if "blockSize" in payload:
                block_size = int(payload["blockSize"])
                if block_size != self.block_size:
                    self.block_size = block_size
                    restart_needed = True

            if "allowFeedback" in payload:
                allow_feedback = bool(payload["allowFeedback"])
                if allow_feedback != self.allow_feedback:
                    self.allow_feedback = allow_feedback
                    restart_needed = True

            if "outputs" in payload and isinstance(payload["outputs"], list):
                by_id = {row.id: row for row in self.output_rows}
                updated_rows: list[OutputSelection] = []
                for item in payload["outputs"]:
                    if not isinstance(item, dict):
                        continue
                    row_id = str(item.get("id") or self._new_row_id())
                    row = by_id.get(row_id) or OutputSelection(id=row_id, device_key=NONE_KEY, volume=FloatSetting(80))
                    if "deviceKey" in item and str(item["deviceKey"]) != row.device_key:
                        row.device_key = str(item["deviceKey"])
                        restart_needed = True
                    if "volume" in item:
                        row.volume.set(self._clamp(float(item["volume"]), 0, 500))
                        volume_changed = True
                    updated_rows.append(row)
                self.output_rows = updated_rows or self.output_rows

            if volume_changed and self.router and self.router.is_alive():
                self.router.set_volumes()
            if restart_needed:
                self._schedule_restart_if_running("setting")
            return self.snapshot_without_refresh()

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.live_restart_timer:
                self.live_restart_timer.cancel()
                self.live_restart_timer = None
            self._poll_router_errors()
            if self.router and self.router.is_alive():
                return self.snapshot_without_refresh()

            source = self._device_by_key(self.sources, self.source_key)
            if not source:
                self.status = "Choose a loopback source."
                return self.snapshot_without_refresh()

            selected_outputs = [
                (row, self._device_by_key(self.outputs, row.device_key))
                for row in self.output_rows
            ]
            if any(device is None for _row, device in selected_outputs):
                self.status = "Choose output devices or set unused rows to None."
                return self.snapshot_without_refresh()

            routes = [
                OutputRoute(device=device, volume_var=row.volume)
                for row, device in selected_outputs
                if device and not device.is_none
            ]
            if not routes:
                self.status = "Choose at least one additional output."
                return self.snapshot_without_refresh()

            duplicate_names = self._duplicate_output_names([route.device for route in routes])
            if duplicate_names:
                self.status = "Each additional output should be selected only once: " + ", ".join(duplicate_names)
                return self.snapshot_without_refresh()

            if not self.allow_feedback:
                for route in routes:
                    if route.device.id == source.id:
                        self.status = "That output is the same device as the loopback source. Set that row to None or enable feedback testing."
                        return self.snapshot_without_refresh()

            self.router = AudioRouter(
                source=source,
                output_routes=routes,
                sample_rate=self.sample_rate,
                block_size=self.block_size,
                master_volume=self.master_volume,
            )
            self.router.start()
            self.status = "Routing current audio..."
            self.last_error = ""
            return self.snapshot_without_refresh()

    def stop(self, status: str = "Stopped.") -> dict[str, Any]:
        with self.lock:
            if self.live_restart_timer:
                self.live_restart_timer.cancel()
                self.live_restart_timer = None
            if self.router:
                self.router.stop()
            self.router = None
            self.status = status
            return self.snapshot_without_refresh()

    def shutdown(self) -> None:
        self.stop("Stopped.")

    def _restart_router(self) -> None:
        with self.lock:
            self.live_restart_timer = None
            if not self.router or not self.router.is_alive():
                return
        self.stop("Restarting audio...")
        self.start()

    def _schedule_restart_if_running(self, reason: str) -> None:
        if not self.router or not self.router.is_alive():
            return
        self.status = f"Applying {reason}..."
        if self.live_restart_timer:
            self.live_restart_timer.cancel()
        self.live_restart_timer = threading.Timer(LIVE_RESTART_MS / 1000.0, self._restart_router)
        self.live_restart_timer.daemon = True
        self.live_restart_timer.start()

    def _poll_router_errors(self) -> None:
        if not self.router:
            return
        while not self.router.error_queue.empty():
            details = self.router.error_queue.get_nowait()
            self.last_error = details
            self.stop(f"Audio routing stopped because of an error. See {ERROR_LOG}.")
            break
        if self.router and self.router.is_alive():
            seconds = self.router.frames_routed / max(1, self.sample_rate)
            skip_text = f", {self.router.skipped_blocks} resync block(s)" if self.router.skipped_blocks else ""
            peak_text = ", hot input" if self.router.peak > 0.98 else ""
            self.status = f"Routing current audio... {seconds:,.1f}s processed{skip_text}{peak_text}"
        elif self.router:
            self.stop()

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

    def _is_running(self) -> bool:
        return bool(self.router and self.router.is_alive())

    def _default_source_key(self, default_speaker_id: str) -> str:
        for source in self.sources:
            if source.id == default_speaker_id:
                return self._device_key(source)
        return self._device_key(self.sources[0]) if self.sources else ""

    @staticmethod
    def _new_row_id() -> str:
        return uuid.uuid4().hex[:10]

    @staticmethod
    def _none_output() -> DeviceChoice:
        return DeviceChoice(label=NONE_LABEL, id="", name=NONE_LABEL, channels=2, is_none=True)

    @staticmethod
    def _device_key(device: DeviceChoice) -> str:
        if device.is_none:
            return NONE_KEY
        prefix = "loopback" if device.is_loopback else "output"
        return f"{prefix}:{device.id}"

    def _device_by_key(self, devices: list[DeviceChoice], key: str) -> DeviceChoice | None:
        return next((device for device in devices if self._device_key(device) == key), None)

    def _serialize_device(self, device: DeviceChoice) -> dict[str, Any]:
        return {
            "key": self._device_key(device),
            "label": device.label,
            "name": device.name,
            "channels": device.channels,
            "isLoopback": device.is_loopback,
            "isNone": device.is_none,
        }

    @staticmethod
    def _contains_device(devices: list[DeviceChoice], target: DeviceChoice) -> bool:
        return any(
            device.id == target.id and device.is_loopback == target.is_loopback and device.is_none == target.is_none
            for device in devices
        )

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

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))


class SplitterRequestHandler(SimpleHTTPRequestHandler):
    control: SplitterControl
    shutdown_event: threading.Event

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self._send_json(self.control.snapshot())
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                self._send_json(self.control.start())
            elif parsed.path == "/api/stop":
                self._send_json(self.control.stop())
            elif parsed.path == "/api/refresh":
                self._send_json(self.control.refresh_devices())
            elif parsed.path == "/api/update":
                self._send_json(self.control.update(self._read_json()))
            elif parsed.path == "/api/outputs":
                self._send_json(self.control.add_output())
            elif parsed.path == "/api/shutdown":
                self._send_json({"ok": True})
                threading.Timer(SHUTDOWN_DELAY_SECONDS, self.shutdown_event.set).start()
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception:
            self._send_json({"error": traceback.format_exc()}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/outputs/"):
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        row_id = unquote(parsed.path.rsplit("/", 1)[-1])
        self._send_json(self.control.remove_output(row_id))

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        if not WEB_DIST.exists():
            self._send_json(
                {
                    "error": "React build is missing.",
                    "hint": "Run npm install and npm run build in the web directory.",
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        relative = path.lstrip("/") or "index.html"
        requested = (WEB_DIST / unquote(relative)).resolve()
        if not str(requested).startswith(str(WEB_DIST.resolve())):
            self._send_json({"error": "Invalid path"}, HTTPStatus.BAD_REQUEST)
            return
        if requested.is_dir():
            requested = requested / "index.html"
        if not requested.exists():
            requested = WEB_DIST / "index.html"

        content_type = mimetypes.guess_type(str(requested))[0] or "application/octet-stream"
        body = requested.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _handler_factory(control: SplitterControl, shutdown_event: threading.Event) -> type[SplitterRequestHandler]:
    class BoundHandler(SplitterRequestHandler):
        pass

    BoundHandler.control = control
    BoundHandler.shutdown_event = shutdown_event
    return BoundHandler


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run(open_mode: str) -> int:
    control = SplitterControl()
    shutdown_event = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), _handler_factory(control, shutdown_event))
    url = f"http://127.0.0.1:{server.server_port}/"
    thread = threading.Thread(target=server.serve_forever, name="react-api", daemon=True)
    thread.start()

    try:
        if open_mode == "webview":
            try:
                import webview

                webview.create_window(APP_TITLE, url, width=1180, height=760, min_size=(940, 660))
                webview.start()
                shutdown_event.set()
            except Exception:
                webbrowser.open(url)
        elif open_mode == "browser":
            webbrowser.open(url)

        while not shutdown_event.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown()
        server.shutdown()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the React Audio Splitter host.")
    parser.add_argument("--browser", action="store_true", help="Open in the default browser instead of an embedded webview.")
    parser.add_argument("--no-open", action="store_true", help="Start the local server without opening a UI window.")
    args = parser.parse_args()
    if args.no_open:
        open_mode = "none"
    elif args.browser:
        open_mode = "browser"
    else:
        open_mode = "webview"
    return run(open_mode)


if __name__ == "__main__":
    raise SystemExit(main())
