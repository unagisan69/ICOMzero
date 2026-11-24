import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop import Context, DesktopClosed, key_code, viewport, TEXT_BASE
from runtime import playback_env, release_asset
from updater import Updater, REPO


class DesktopInput(unittest.TestCase):
    def setUp(self):
        self.ctx = Context.__new__(Context)
        self.ctx.event_flags = 2
        self.ctx.events = __import__("collections").deque()
        self.ctx.held = {}
        self.ctx.releases = {}
        self.ctx.closing = False
        self.ctx.display = SimpleNamespace(text_input=False)
        self.ctx.root = Mock()
        self.ctx.root.after.return_value = "pending-release"

    def event(self, key="z", char="z", code=52, state=0):
        return SimpleNamespace(keysym=key, char=char, keycode=code, state=state)

    def test_keyboard_commands_and_native_text_are_distinct(self):
        self.assertEqual(key_code("z", "z"), 44)
        self.assertEqual(key_code("Up"), 33)
        self.assertEqual(key_code("Control_L"), 389)
        self.assertEqual(key_code("F3"), 0x7002)
        for symbol in (".", ":", "-", "@", "[", "A", "é", "中", "🛰"):
            code = key_code(symbol, symbol, text_input=True)
            app = runpy.run_path(
                str(Path(__file__).resolve().parents[1] / "app/icomzero")
            )
            self.assertEqual(app["text_character"](code), symbol)
        # Native punctuation never passes through the Cardputer symbol map.
        self.assertEqual(key_code("bracketleft", "["), TEXT_BASE + ord("["))

    def test_repeat_does_not_reset_hold_and_focus_loss_releases_keys(self):
        self.ctx.press(self.event())
        self.ctx.release(self.event())
        self.ctx.press(self.event())
        self.assertEqual(list(self.ctx.events), [(44, 1), (44, 2)])
        self.ctx.root.after_cancel.assert_called_once_with("pending-release")
        self.ctx.press(self.event("Control_L", "", 37))
        self.ctx.focus_out(None)
        self.assertEqual(list(self.ctx.events)[-2:], [(44, 0), (389, 0)])
        self.assertFalse(self.ctx.held)

    def test_close_raises_through_nested_keyboard_poll(self):
        self.ctx.root.tk.dooneevent.side_effect = lambda *_: self.ctx.request_close()
        with self.assertRaises(DesktopClosed):
            self.ctx.poll(timeout=0)

    def test_nested_forms_keep_queued_keystrokes_in_order(self):
        self.ctx.events.extend([(28, 1), (28, 0), (TEXT_BASE + ord("A"), 1)])
        self.assertEqual(self.ctx.poll(timeout=0), [(28, 1)])
        self.assertEqual(self.ctx.poll(timeout=0), [(28, 0)])
        self.assertEqual(self.ctx.poll(timeout=0), [(TEXT_BASE + ord("A"), 1)])

    def test_fullscreen_repeat_never_reinterprets_held_enter_as_a_command(self):
        event = self.event("Return", "", code=36, state=4)
        self.ctx.press(event)
        self.ctx.release(event)
        self.ctx.press(event)
        self.ctx.press(self.event("Return", "", code=36, state=0))
        self.assertEqual(list(self.ctx.events), [])
        self.assertEqual(self.ctx.root.attributes.call_count, 2)
        self.ctx.finish_release(36)
        self.ctx.press(self.event("Return", "", code=36, state=0))
        self.assertEqual(list(self.ctx.events), [(28, 1)])

    def test_arrow_first_navigation_and_control_actions(self):
        for symbol, expected in (
            ("Up", 103),
            ("Down", 108),
            ("Left", 114),
            ("Right", 115),
            ("m", 113),
        ):
            self.ctx.held.clear()
            self.ctx.events.clear()
            self.ctx.press(self.event(symbol, "", code=20, state=4))
            self.assertEqual(list(self.ctx.events), [(expected, 1)])
        self.assertEqual(key_code("Up"), 33)
        self.assertEqual(key_code("Down"), 45)
        self.assertEqual(key_code("Left"), 105)
        self.assertEqual(key_code("Right"), 106)

    def test_text_mode_preserves_caps_and_shifted_characters(self):
        self.ctx.display.text_input = True
        self.ctx.press(self.event("A", "A"))
        self.assertEqual(list(self.ctx.events), [(TEXT_BASE + ord("A"), 1)])

    def test_text_arrows_never_insert_navigation_shortcut_letters(self):
        self.assertEqual(key_code("Up", text_input=True), 103)
        self.assertEqual(key_code("Down", text_input=True), 108)

    def test_clipboard_paste_is_text_only_and_not_a_command(self):
        self.ctx.display.text_input = True
        self.ctx.root.clipboard_get.return_value = "API:key.123"
        self.ctx.press(self.event("v", "", state=4))
        inserted = "".join(
            chr(key - TEXT_BASE) for key, value in self.ctx.events if value == 1
        )
        self.assertEqual(inserted, "API:key.123")
        self.assertNotIn((47, 1), self.ctx.events)

    def test_viewport_preserves_aspect_and_letterboxes(self):
        self.assertEqual(viewport(1280, 680), (0, 0, 1280, 680))
        x, y, w, h = viewport(1280, 720)
        self.assertEqual((x, y), (0, 20))
        self.assertEqual((w, h), (1280, 680))


class DesktopRuntime(unittest.TestCase):
    def test_audio_uses_system_default_on_desktop_and_cardputer_sink_on_cardputer(self):
        with patch.dict(os.environ, {"ICOMZERO_PLATFORM": "desktop"}, clear=True):
            self.assertNotIn("PULSE_SINK", playback_env())
            self.assertEqual(
                release_asset("0.18.0"), "icomzero-desktop_0.18.0-1_arm64.deb"
            )
        with patch.dict(
            os.environ,
            {"ICOMZERO_PLATFORM": "desktop", "PULSE_SINK": "custom"},
            clear=True,
        ):
            self.assertEqual(playback_env()["PULSE_SINK"], "custom")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                playback_env()["PULSE_SINK"],
                "alsa_output.platform-sound.stereo-fallback",
            )
            self.assertEqual(release_asset("0.18.0"), "icomzero_0.18.0-1_arm64.deb")

    def test_update_installer_rejects_cardputer_package_on_desktop(self):
        import hashlib, tempfile
        from update_install import install

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.deb"
            path.write_bytes(b"fixture")
            fields = {
                "Package": "icomzero",
                "Architecture": "arm64",
                "Version": "0.18.0",
                "X-ICOMzero-Platform": "",
            }
            with patch("update_install.os.geteuid", return_value=0), patch(
                "runtime.target", return_value="desktop"
            ), patch(
                "update_install.subprocess.check_output",
                side_effect=lambda argv, **_: fields[argv[-1]],
            ):
                with self.assertRaisesRegex(ValueError, "Wrong platform"):
                    install(
                        path, hashlib.sha256(path.read_bytes()).hexdigest(), "0.18.0"
                    )

    def test_desktop_updater_requires_desktop_asset(self):
        name = "icomzero-desktop_0.18.1-1_arm64.deb"

        def release(asset):
            return json.dumps(
                {
                    "tag_name": "v0.18.1",
                    "assets": [
                        {
                            "name": n,
                            "browser_download_url": REPO
                            + "/releases/download/v0.18.1/"
                            + n,
                        }
                        for n in (asset, "SHA256SUMS")
                    ],
                }
            ).encode()

        with patch.dict(os.environ, {"ICOMZERO_PLATFORM": "desktop"}), patch(
            "updater.fetch", return_value=release(name)
        ):
            self.assertTrue(Updater("0.18.0").check())
        with patch.dict(os.environ, {"ICOMZERO_PLATFORM": "desktop"}), patch(
            "updater.fetch", return_value=release("icomzero_0.18.1-1_arm64.deb")
        ):
            with self.assertRaises(ValueError):
                Updater("0.18.0").check()
