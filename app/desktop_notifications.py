"""Best-effort desktop alerts; never block radio or UI threads."""

import queue
import subprocess
import threading
import time


class DesktopAlerts:
    def __init__(self):
        self.events = queue.Queue(maxsize=12)
        self.closed = threading.Event()
        self.last_errors = {}
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def send(self, title, message, error=False):
        if self.closed.is_set():
            return
        key = (title, message)
        now = time.monotonic()
        if error:
            self.last_errors = {
                k: t for k, t in self.last_errors.items() if now - t < 60
            }
            if key in self.last_errors:
                return
            self.last_errors[key] = now
        try:
            self.events.put_nowait((title, message, error))
        except queue.Full:
            pass

    def run(self):
        while not self.closed.is_set():
            try:
                title, message, error = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                subprocess.run(
                    [
                        "notify-send",
                        "--app-name=ICOMzero",
                        "--hint=string:desktop-entry:icomzero",
                        "--urgency=" + ("critical" if error else "normal"),
                        "--expire-time=8000",
                        "--",
                        title,
                        message,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self):
        self.closed.set()
        self.worker.join(timeout=3.3)
