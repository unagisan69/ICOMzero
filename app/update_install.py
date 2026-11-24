"""Root helper: stage and validate the exact downloaded package before installing."""

import hashlib
import json
import time
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def emit_event(event):
    # Closing the app must never interrupt an already-running package install.
    # Use unbuffered writes so a closed UI pipe cannot fail at interpreter exit.
    try:
        os.write(sys.stdout.fileno(), (json.dumps(event) + "\n").encode())
    except OSError:
        pass


class Progress:
    def __init__(self, emit=lambda event: None):
        self.emit = emit
        self.current = None
        self.started = None

    def enter(self, stage, percent=None):
        if stage != self.current:
            self.finish("ok")
            self.current = stage
            self.started = time.monotonic()
        self.emit(dict(event="progress", stage=stage, percent=percent))

    def finish(self, status):
        if self.current:
            self.emit(
                dict(
                    event="timing",
                    stage=self.current,
                    seconds=round(time.monotonic() - self.started, 3),
                    status=status,
                )
            )
            self.current = None


def apt_progress(line):
    # APT's documented Status-Fd format. Do not forward descriptions/paths.
    parts = line.split(":", 3)
    if len(parts) != 4:
        return None
    stage = {"dlstatus": "dependencies", "pmstatus": "installing"}.get(parts[0])
    if not stage:
        return None
    try:
        percent = float(parts[2])
    except ValueError:
        return None
    if not 0 <= percent <= 100:
        return None
    return stage, percent


def run_apt(target, log, report):
    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [
                "apt-get",
                "-o",
                f"APT::Status-Fd={write_fd}",
                "install",
                "-y",
                "--no-remove",
                str(target),
            ],
            pass_fds=(write_fd,),
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, "r", errors="replace") as stream:
        for line in stream:
            progress = apt_progress(line.strip())
            if progress:
                report.enter(*progress)
                if progress[0] == "dependencies" and progress[1] >= 100:
                    report.enter("prepare_install")
    result = process.wait()
    if result:
        raise subprocess.CalledProcessError(result, "apt-get install")


def private_cache(path):
    path = Path(path)
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unsafe installation cache")
    path.chmod(0o700)
    return path


def install(path, digest, version, report=None, cache_dir=None):
    report = report or Progress()
    try:
        _install(path, digest, version, report, cache_dir)
    except BaseException:
        report.finish("error")
        raise
    else:
        report.finish("ok")


def _install(path, digest, version, report, cache_dir):
    if os.geteuid() != 0:
        raise ValueError("Administrator access required")
    if not re.fullmatch("[0-9a-f]{64}", digest) or not re.fullmatch(
        r"\d+\.\d+\.\d+", version
    ):
        raise ValueError("Invalid release metadata")
    report.enter("stage_package")
    with tempfile.TemporaryDirectory(
        prefix="icomzero-install-", dir=cache_dir
    ) as directory:
        target = Path(directory) / "icomzero.deb"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as src, target.open("wb") as dst:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
                raise ValueError("Invalid package file")
            total = 0
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    raise ValueError("Package too large")
                dst.write(chunk)
        report.enter("verify_package")
        sha = hashlib.sha256()
        with target.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)
        if sha.hexdigest() != digest:
            raise ValueError("Checksum mismatch")
        report.enter("metadata")
        for field, expected in [
            ("Package", "icomzero"),
            ("Architecture", "arm64"),
            ("Version", version),
        ]:
            value = subprocess.check_output(
                ["dpkg-deb", "-f", str(target), field], text=True
            ).strip()
            if value != expected:
                raise ValueError("Wrong package metadata")
        from runtime import target as runtime_target

        if runtime_target() == "desktop":
            platform = subprocess.check_output(
                ["dpkg-deb", "-f", str(target), "X-ICOMzero-Platform"], text=True
            ).strip()
            if platform != "desktop":
                raise ValueError("Wrong platform package")
        installed = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", "icomzero"], text=True
        ).strip()
        if (
            subprocess.run(
                ["dpkg", "--compare-versions", version, "lt", installed]
            ).returncode
            == 0
        ):
            raise ValueError("Downgrade refused")
        log_fd = os.open(
            "/var/log/icomzero-update.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(log_fd, 0o600)
        with os.fdopen(log_fd, "ab") as log:
            started = time.monotonic()
            status = "error"
            log.write(b"\nICOMzero update installation started\n")
            log.flush()
            try:
                report.enter("prepare_install")
                run_apt(target, log, report)
                report.enter("finish")
                status = "ok"
            finally:
                log.write(
                    (
                        "ICOMzero installation seconds=%.3f status=%s\n"
                        % (time.monotonic() - started, status)
                    ).encode()
                )
                log.flush()


if __name__ == "__main__":
    try:
        install(
            *sys.argv[1:],
            report=Progress(emit_event),
            cache_dir=private_cache("/var/cache/icomzero"),
        )
    except Exception:
        sys.exit(1)
