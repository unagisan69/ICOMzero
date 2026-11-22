"""Non-blocking UI state for checked, explicitly requested GitHub updates."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

from updater import Updater
from profiles import config_path


class UpdateSession:
    def __init__(self):
        self.updater = Updater(log_path=config_path().parent / "update-timing.jsonl")
        self.state = "idle"
        self.stage = ""
        self.received = self.total = None
        self.error = ""
        self.cancelled = threading.Event()
        self.thread = None

    def progress(self, stage, received, total):
        if self.cancelled.is_set():
            raise ValueError("Update cancelled")
        self.stage, self.received, self.total = stage, received, total

    def run(self, operation, target):
        if self.thread and self.thread.is_alive():
            return
        self.cancelled.clear()
        self.error = ""
        self.state = operation

        def work():
            try:
                target()
            except Exception as error:
                self.error = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Update unavailable: " + type(error).__name__
                )
                self.state = "error"
            finally:
                if self.cancelled.is_set():
                    self.updater.cleanup()
                    self.state = "idle"

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def check(self, startup=False):
        def check():
            try:
                process = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("updater.py")),
                        "--check",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=7,
                )
                result = (
                    json.loads(process.stdout)
                    if process.returncode == 0
                    else {"error": True}
                )
                if result.get("error"):
                    raise ValueError("Update check unavailable; check Wi-Fi or retry")
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                if startup:
                    self.state = "offline"
                    return
                raise ValueError(
                    "Update check unavailable; check Wi-Fi or retry"
                ) from error
            release = result.get("release")
            self.updater.release = tuple(release) if release else None
            self.state = "available" if release else "current"

        self.run("checking", check)

    def download(self):
        def download():
            self.updater.download(self.progress)
            self.state = "ready"

        self.run("downloading", download)

    def install(self, password):
        def install():
            asyncio.run(self.updater.install(password, self.progress))
            self.state = "done"

        self.run("installing", install)

    def cancel(self):
        if self.state == "installing":
            return
        self.cancelled.set()
        if not self.thread or not self.thread.is_alive():
            self.updater.cleanup()
            self.state = "idle"
