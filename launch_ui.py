from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


APP_TITLE = "Dual Output Router"
ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "launcher_error.log"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
VENV_PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} Launcher")
        self.resizable(False, False)
        self.configure(bg="#f5f7fb")
        self.protocol("WM_DELETE_WINDOW", self._close_requested)

        self.messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.stop_requested = False

        self._build_ui()
        self.after(100, self._poll_messages)
        threading.Thread(target=self._run_startup, name="startup", daemon=True).start()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Splash.TFrame", background="#f5f7fb")
        style.configure("Title.TLabel", background="#f5f7fb", foreground="#202532", font=("Segoe UI", 17, "bold"))
        style.configure("Status.TLabel", background="#f5f7fb", foreground="#4b5565", font=("Segoe UI", 10))

        frame = ttk.Frame(self, style="Splash.TFrame", padding=24)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Launching", style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="Starting checks...")
        ttk.Label(frame, textvariable=self.status_var, style="Status.TLabel", width=52).pack(anchor="w", pady=(10, 14))

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=360)
        self.progress.pack(fill="x")
        self.progress.start(12)

        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f"+{x}+{y}")

    def _run_startup(self) -> None:
        try:
            self._status("Checking Splitter Output driver...")
            driver_result = self._ensure_driver()
            if driver_result == 100:
                self._status("Driver setup is continuing in the administrator window...")
                time.sleep(1.5)
                self._done()
                return
            if driver_result == 101:
                self._done()
                return

            self._status("Preparing Python environment...")
            self._ensure_venv()

            self._status("Installing/updating audio dependencies...")
            self._install_requirements()

            self._status("Opening audio router...")
            self._launch_app()
            self._done()
        except Exception as exc:
            self._error(str(exc))

    def _ensure_driver(self) -> int:
        powershell = self._which("powershell")
        if not powershell:
            self._log("PowerShell was not found; skipping driver auto-setup.")
            return 0

        setup_script = ROOT / "setup_windows.ps1"
        result = self._run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(setup_script),
                "-FromLauncher",
                "-EnsureDriverOnly",
                "-StartAppWhenDone",
            ],
            check=False,
        )
        if result.returncode in (0, 100, 101):
            return result.returncode
        raise RuntimeError("Driver setup failed. See launcher_error.log for details.")

    def _ensure_venv(self) -> None:
        if VENV_PYTHON.exists() and VENV_PYTHONW.exists():
            return
        self._run([sys.executable, "-m", "venv", str(ROOT / ".venv")])

    def _install_requirements(self) -> None:
        self._run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
        self._run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    def _launch_app(self) -> None:
        subprocess.Popen(
            [str(VENV_PYTHONW), str(ROOT / "dual_output_router.py")],
            cwd=str(ROOT),
            close_fds=True,
            creationflags=self._creation_flags(),
        )

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            creationflags=self._creation_flags(),
        )
        self._log_command(args, result)
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(args)}")
        return result

    def _status(self, text: str) -> None:
        self.messages.put(("status", text))

    def _done(self) -> None:
        self.messages.put(("done", None))

    def _error(self, text: str) -> None:
        self._log(text)
        self.messages.put(("error", text))

    def _poll_messages(self) -> None:
        while not self.messages.empty():
            kind, value = self.messages.get_nowait()
            if kind == "status" and value:
                self.status_var.set(value)
            elif kind == "done":
                self.destroy()
                return
            elif kind == "error" and value:
                self.progress.stop()
                self.status_var.set("Launch failed.")
                messagebox.showerror(APP_TITLE, f"{value}\n\nDetails were written to:\n{LOG_FILE}")
                self.destroy()
                return
        self.after(100, self._poll_messages)

    def _close_requested(self) -> None:
        self.stop_requested = True
        self.destroy()

    @staticmethod
    def _creation_flags() -> int:
        if sys.platform == "win32":
            return subprocess.CREATE_NO_WINDOW
        return 0

    @staticmethod
    def _which(name: str) -> str | None:
        from shutil import which

        return which(name)

    def _log_command(self, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        command = " ".join(args)
        self._log(
            f"$ {command}\n"
            f"exit={result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n"
        )

    @staticmethod
    def _log(text: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"\n[{timestamp}]\n{text}\n")


if __name__ == "__main__":
    Launcher().mainloop()
