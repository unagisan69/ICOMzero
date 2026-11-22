"""GitHub releases updater; no account, telemetry, or device credentials persisted."""

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen
from version import VERSION as __version__
from update_log import TimingLog

REPO = "https://github.com/unagisan69/ICOMzero"
API = "https://api.github.com/repos/unagisan69/ICOMzero/releases/latest"
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
INSTALL_STAGES = {
    "stage_package",
    "verify_package",
    "metadata",
    "prepare_install",
    "dependencies",
    "installing",
    "finish",
}


def version(value):
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Unsupported release version")
    return tuple(map(int, value.lstrip("v").split(".")))


def open_download(url, timeout=20):
    url += ("&" if "?" in url else "?") + "icomzero=" + str(time.time_ns())
    response = urlopen(
        Request(
            url,
            headers={
                "User-Agent": "ICOMzero-updater",
                "Accept": "application/vnd.github+json",
                "Cache-Control": "no-cache",
            },
        ),
        timeout=timeout,
    )
    if not response.url.startswith("https://"):
        response.close()
        raise ValueError("Insecure download refused")
    return response


def fetch(url, limit, timeout=20):
    with open_download(url, timeout) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Download exceeds size limit")
        return data


class Updater:
    def __init__(self, current=__version__, log_path=None, cache_dir=None):
        self.timings = TimingLog(log_path)
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            / "icomzero-updates"
        )
        self.current = current
        self.release = None
        self.directory = None
        self.path = None
        self.digest = None

    @contextmanager
    def stage(self, name, progress=None):
        started = time.monotonic()
        status = "error"
        if progress:
            progress(name, None, None)
        try:
            yield
            status = "ok"
        finally:
            self.timings.record(name, time.monotonic() - started, status)

    def check(self, timeout=8, progress=None):
        with self.stage("check", progress):
            return self._check(timeout)

    def _check(self, timeout):
        release = json.loads(fetch(API, 1024 * 1024, timeout=timeout))
        tag = release["tag_name"]
        if release.get("draft") or release.get("prerelease"):
            raise ValueError("No published update available")
        self.release = None
        if version(tag) <= version(self.current):
            return False
        name = f'icomzero_{tag.lstrip("v")}-1_arm64.deb'
        assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
        for asset in (name, "SHA256SUMS"):
            if assets.get(asset) != f"{REPO}/releases/download/{tag}/{asset}":
                raise ValueError("Release files are incomplete")
        self.release = (tag.lstrip("v"), name, assets[name], assets["SHA256SUMS"])
        return True

    def download(self, progress=None):
        if not self.release:
            raise ValueError("Check for an update first")
        ver, name, url, sums = self.release
        with self.stage("manifest", progress):
            lines = fetch(sums, 65536).decode().splitlines()
            matches = [
                line.split()[0]
                for line in lines
                if len(line.split()) == 2 and line.split()[1].lstrip("*") == name
            ]
            if len(matches) != 1 or not re.fullmatch("[0-9a-f]{64}", matches[0]):
                raise ValueError("Release checksum missing")
        self.cleanup()
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache_dir.chmod(0o700)
        self.directory = Path(
            tempfile.mkdtemp(prefix="icomzero-update-", dir=self.cache_dir)
        )
        self.path = self.directory / name
        try:
            self.download_package(url, progress)
            with self.stage("verify_download", progress):
                sha = hashlib.sha256()
                with self.path.open("rb") as f:
                    while chunk := f.read(1024 * 1024):
                        sha.update(chunk)
                self.digest = sha.hexdigest()
                if self.digest != matches[0]:
                    raise ValueError("Download checksum mismatch")
        except Exception:
            self.cleanup()
            raise

    def download_package(self, url, progress=None):
        limit = MAX_PACKAGE_BYTES
        with self.stage("connect", progress):
            response = open_download(url)
        with response:
            header = response.headers.get("Content-Length")
            try:
                total = int(header) if header is not None else None
            except ValueError:
                total = None
            if total is not None and (total < 0 or total > limit):
                raise ValueError("Download exceeds size limit")
            with self.stage("download", progress):
                received = 0
                last_report = 0
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    if progress:
                        progress("download", 0, total)
                    while chunk := response.read(64 * 1024):
                        received += len(chunk)
                        if received > limit:
                            raise ValueError("Download exceeds size limit")
                        f.write(chunk)
                        now = time.monotonic()
                        if progress and now - last_report >= 0.1:
                            progress("download", received, total)
                            last_report = now
                if total is not None and received != total:
                    raise ValueError("Incomplete download")
                if progress:
                    progress("download", received, total)

    async def install(self, password="", progress=None):
        if not self.path:
            raise ValueError("Download an update first")
        helper = Path(__file__).with_name("update_install.py")
        started = time.monotonic()
        auth_started = started
        first_event = True
        status = "error"
        if progress:
            progress("authenticate", None, None)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "sudo",
                "-S",
                "-p",
                "",
                "/usr/bin/python3",
                str(helper),
                str(self.path),
                self.digest,
                self.release[0],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            process.stdin.write((password + "\n").encode())
            password = ""
            await process.stdin.drain()
            process.stdin.close()
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                name = event.get("stage") if isinstance(event, dict) else None
                if name not in INSTALL_STAGES:
                    continue
                if first_event:
                    self.timings.record("authenticate", time.monotonic() - auth_started)
                    first_event = False
                if event.get("event") == "timing":
                    seconds = event.get("seconds")
                    if isinstance(seconds, (int, float)) and 0 <= seconds <= 86400:
                        self.timings.record(
                            name,
                            seconds,
                            "ok" if event.get("status") == "ok" else "error",
                        )
                elif event.get("event") == "progress" and progress:
                    value = event.get("percent")
                    percent = (
                        max(0, min(100, value))
                        if isinstance(value, (int, float))
                        else None
                    )
                    progress(name, percent, 100 if percent is not None else None)
            await process.wait()
            if process.returncode:
                raise ValueError("Install failed. Check password / update log.")
            status = "ok"
            self.cleanup()
        finally:
            password = ""
            if first_event:
                self.timings.record(
                    "authenticate", time.monotonic() - auth_started, "error"
                )
            self.timings.record("install_total", time.monotonic() - started, status)

    def cleanup(self):
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.directory = self.path = None


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--check"]:
        updater = Updater()
        try:
            print(
                json.dumps(
                    {"release": updater.release if updater.check(timeout=4) else None}
                )
            )
        except Exception:
            print(
                json.dumps({"error": "Update check unavailable; check Wi-Fi or retry"})
            )
