"""Stock Cardputer symbol events, without accessing physical input devices."""

import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch


class KeyboardText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )

    def test_stock_sym_b_dot_and_url_punctuation(self):
        char = self.module["text_character"]
        self.assertEqual(char(91), ".")  # Physical SYM+B, not Linux KEY_DOT.
        self.assertEqual(char(91, True), ".")
        self.assertEqual(
            "".join(char(code) for code in (80, 92, 92, 70, 71, 93, 73, 51)), "://_-?=&"
        )
        self.assertEqual(
            "".join(char(code) for code in (26, 27, 39, 40, 41, 43, 52, 53, 94)),
            "!@#$%^*()",
        )
        self.assertEqual(char(48), "b")
        self.assertEqual(char(48, True), "B")
        self.assertIsNone(char(15))

    def test_profile_editor_saves_ipv4_from_stock_symbol_events(self):
        import tempfile
        from types import SimpleNamespace
        from PIL import ImageFont
        from profiles import ProfileStore
        from settings_ui import SettingsScreens

        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        display = SimpleNamespace(
            fonts={
                k: ImageFont.truetype(font, n)
                for k, n in (("small", 12), ("title", 22))
            },
            present=lambda image: None,
        )
        keys = iter([45, 28, 2, 10, 3, 91, 11, 91, 3, 91, 2, 3, 4, 28, 1])
        with tempfile.TemporaryDirectory() as root:
            store = ProfileStore(Path(root) / "radio.json")
            screens = SettingsScreens(
                display,
                -1,
                lambda _: [(next(keys), 1)],
                self.module["text_character"],
                store,
            )
            screens.edit_radio("legacy")
            self.assertEqual(store.config()["Host"], "192.0.2.123")
            self.assertEqual(store.config()["Password"], "")

    def test_s_opens_radio_settings_and_changes_step_while_receiving(self):
        import tempfile
        from profiles import ProfileStore

        module = self.module
        with tempfile.TemporaryDirectory() as root:
            store = ProfileStore(Path(root) / "radio.json")
            store.save_connection(
                ["192.0.2.1", "50001", "", "", "Auto (Icom)", "60001", "User"]
            )
            with patch.dict(module["ui"].__globals__, {"CONFIG": store.path}):
                receiver = module["Receiver"]()
                receiver.thread = Mock()
                receiver.thread.is_alive.side_effect = (
                    lambda: not receiver.stop.is_set()
                )
                keys = [31, 45, 28, 1, 1]
                read, write = os.pipe()
                try:
                    os.write(
                        write,
                        b"".join(
                            module["EVENT"].pack(0, 0, 1, key, value)
                            for key in keys
                            for value in (1, 0)
                        ),
                    )
                    with patch.dict(
                        module["ui"].__globals__,
                        {"Display": lambda _: Mock(), "Receiver": lambda: receiver},
                    ):
                        module["ui"](-1, read)
                finally:
                    os.close(read)
                    os.close(write)
            self.assertEqual(receiver.step, 500)
            self.assertEqual(store.active()["step"], 500)
            self.assertEqual(receiver.volume, 60)
