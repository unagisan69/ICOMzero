import asyncio
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from updater import Updater, REPO, version, open_download
from update_install import install, apt_progress, Progress, emit_event
from update_session import UpdateSession


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = (
            headers if headers is not None else {"Content-Length": str(len(data))}
        )
        self.url = "https://example.invalid/package"


class Updates(unittest.TestCase):
    def release(self):
        return json.dumps(
            {
                "tag_name": "v0.15.1",
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": REPO
                        + "/releases/download/v0.15.1/"
                        + name,
                    }
                    for name in ("icomzero_0.15.1-1_arm64.deb", "SHA256SUMS")
                ],
            }
        ).encode()

    def test_new_release_requires_canonical_files_and_valid_version(self):
        self.assertGreater(version("0.15.10"), version("v0.15.2"))
        with self.assertRaises(ValueError):
            version("0.15.1; bad")
        with patch("updater.fetch", return_value=self.release()):
            self.assertTrue(Updater("0.15.0").check())
            self.assertFalse(Updater("0.15.1").check())
        data = json.loads(self.release())
        data["assets"][0]["browser_download_url"] = "https://example.invalid/other.deb"
        with patch("updater.fetch", return_value=json.dumps(data).encode()):
            with self.assertRaises(ValueError):
                Updater("0.15.0").check()

    def test_verified_download_reports_bytes_and_rejects_damage(self):
        payload = b"x" * (128 * 1024 + 17)
        for headers in ({"Content-Length": str(len(payload))}, {}):
            with tempfile.TemporaryDirectory() as root:
                u = Updater("0.15.0", cache_dir=root)
                with patch("updater.fetch", return_value=self.release()):
                    u.check()
                manifest = (
                    hashlib.sha256(payload).hexdigest() + "  " + u.release[1] + "\n"
                ).encode()
                progress = []
                with patch("updater.fetch", return_value=manifest), patch(
                    "updater.open_download", return_value=Response(payload, headers)
                ):
                    u.download(lambda *e: progress.append(e))
                self.assertEqual(u.path.read_bytes(), payload)
                self.assertEqual(u.path.stat().st_mode & 0o777, 0o600)
                self.assertIn(
                    ("download", len(payload), len(payload) if headers else None),
                    progress,
                )
                u.cleanup()
                with patch("updater.fetch", return_value=manifest), patch(
                    "updater.open_download", return_value=Response(b"bad")
                ):
                    with self.assertRaises(ValueError):
                        u.download()
                self.assertIsNone(u.path)

    def test_size_limit_truncation_and_insecure_redirect(self):
        for payload, headers, limit in (
            (b"abc", {"Content-Length": "4"}, 100),
            (b"abcdef", {}, 5),
        ):
            with tempfile.TemporaryDirectory() as root:
                u = Updater("0.15.0", cache_dir=root)
                with patch("updater.fetch", return_value=self.release()):
                    u.check()
                manifest = (
                    hashlib.sha256(payload).hexdigest() + "  " + u.release[1] + "\n"
                ).encode()
                with patch("updater.fetch", return_value=manifest), patch(
                    "updater.open_download", return_value=Response(payload, headers)
                ), patch("updater.MAX_PACKAGE_BYTES", limit):
                    with self.assertRaises(ValueError):
                        u.download()
                self.assertIsNone(u.path)
        response = Response(b"bad")
        response.url = "http://example.invalid/package"
        with patch("updater.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                open_download("https://example.invalid/package")
        self.assertTrue(response.closed)

    def test_privileged_helper_rejects_hash_wrong_package_and_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "update.deb"
            path.write_bytes(b"package")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("update_install.os.geteuid", return_value=0):
                with self.assertRaises(ValueError):
                    install(str(path), "0" * 64, "0.15.1")
                with patch(
                    "update_install.subprocess.check_output",
                    return_value="wrong-package",
                ):
                    with self.assertRaises(ValueError):
                        install(str(path), digest, "0.15.1")
                link = Path(root) / "link"
                link.symlink_to(path)
                with self.assertRaises(OSError):
                    install(str(link), digest, "0.15.1")

    def test_apt_progress_and_closed_ui_do_not_leak_descriptions_or_abort(self):
        self.assertEqual(
            apt_progress("pmstatus:pkg:42:private details"), ("installing", 42)
        )
        for value in (
            "pmstatus:pkg:nan:text",
            "pmstatus:pkg:101:text",
            "pmerror:pkg:0:secret",
        ):
            self.assertIsNone(apt_progress(value))
        with patch("update_install.os.write", side_effect=BrokenPipeError):
            report = Progress(emit_event)
            report.enter("installing", 42)
            report.finish("ok")

    def test_startup_check_timeout_is_bounded_and_does_not_block_caller(self):
        session = UpdateSession()
        with patch(
            "update_session.subprocess.run",
            side_effect=subprocess.TimeoutExpired("check", 7),
        ) as check:
            session.check(startup=True)
            session.thread.join(2)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(check.call_args.kwargs["timeout"], 7)
        self.assertEqual(session.state, "offline")
        self.assertIsNone(session.updater.release)


class InstallProgress(unittest.IsolatedAsyncioTestCase):
    async def test_password_is_only_sent_to_sudo_and_stages_reach_ui(self):
        with tempfile.TemporaryDirectory() as root:
            u = Updater(log_path=Path(root) / "update.log")
            u.path = Path(root) / "package"
            u.digest = "a" * 64
            u.release = ("0.15.1",)
            event = (
                json.dumps(
                    {"event": "progress", "stage": "installing", "percent": 42}
                ).encode()
                + b"\n"
            )
            process = SimpleNamespace(
                stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()),
                stdout=SimpleNamespace(readline=AsyncMock(side_effect=[event, b""])),
                returncode=0,
                wait=AsyncMock(),
            )
            progress = []
            with patch(
                "updater.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ) as run:
                await u.install("test-device-password", lambda *e: progress.append(e))
            self.assertEqual(run.call_args.args[0], "sudo")
            self.assertTrue(run.call_args.kwargs["start_new_session"])
            self.assertNotIn("test-device-password", str(run.call_args))
            process.stdin.write.assert_called_once_with(b"test-device-password\n")
            self.assertIn(("installing", 42, 100), progress)
            self.assertNotIn(
                "test-device-password", (Path(root) / "update.log").read_text()
            )
