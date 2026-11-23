import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PIL import ImageFont
from profiles import ProfileStore
from settings_ui import SettingsScreens, RestartApp


class SettingsInterface(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.store = ProfileStore(Path(self.root.name) / "radio.json")
        self.module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        self.display = SimpleNamespace(
            fonts={
                key: ImageFont.truetype(font, size)
                for key, size in (("title", 22), ("body", 14), ("small", 12))
            },
            present=Mock(),
        )

    def test_main_menu_settings_card_opens_general_settings(self):
        self.store.preference("check_updates", False)
        update = SimpleNamespace(state="idle", cancel=Mock(), check=Mock())
        read, write = os.pipe()
        try:
            os.write(
                write,
                b"".join(
                    self.module["EVENT"].pack(0, 0, 1, key, 1)
                    for key in (46, 46, 46, 28, 1)
                ),
            )
            with patch.dict(
                self.module["home"].__globals__,
                {"Display": lambda _: self.display, "CONFIG": self.store.path},
            ), patch("settings_ui.SettingsScreens") as screens, patch(
                "update_session.UpdateSession", return_value=update
            ):
                self.module["home"](-1, read)
            screens.return_value.general.assert_called_once_with(update)
            update.check.assert_not_called()
        finally:
            os.close(read)
            os.close(write)
        images = [
            self.module["home_frame"](self.display, i, "Test radio") for i in range(4)
        ]
        self.assertEqual(len({im.tobytes() for im in images}), 4)
        self.assertTrue(all(im.size == (320, 170) for im in images))

    def test_main_menu_log_opens_without_connecting_to_radio(self):
        self.store.preference("check_updates", False)
        read, write = os.pipe()
        try:
            os.write(
                write,
                b"".join(
                    self.module["EVENT"].pack(0, 0, 1, key, 1)
                    for key in (46, 46, 28, 1)
                ),
            )
            with patch.dict(
                self.module["home"].__globals__,
                {"Display": lambda _: self.display, "CONFIG": self.store.path},
            ), patch("log_ui.LogScreens") as log, patch(
                "update_session.UpdateSession"
            ) as update:
                update.return_value.state = "idle"
                self.module["home"](-1, read)
            log.return_value.browser.assert_called_once()
        finally:
            os.close(read)
            os.close(write)

    def test_general_settings_exposes_shared_station_and_logging(self):
        screen = SettingsScreens(
            self.display, -1, lambda *_: [], self.module["text_character"], self.store
        )
        with patch.object(screen, "select", side_effect=[3, None]), patch.object(
            screen, "logging"
        ) as logging:
            screen.general()
        logging.assert_called_once()

    def test_step_applies_to_tuning_and_is_kept_per_profile(self):
        self.store.update("legacy", step=500)
        remote = self.store.add("legacy")
        self.store.update(remote, step=100)
        for profile, step in (("legacy", 500), (remote, 100)):
            self.store.select(profile)
            with patch.dict(
                self.module["Receiver"].__init__.__globals__,
                {"CONFIG": self.store.path},
            ):
                receiver = self.module["Receiver"]()
            receiver.thread = SimpleNamespace(is_alive=lambda: True)
            receiver.frequency = 7074000
            receiver.tune(steps=1)
            self.assertEqual(receiver.target, 7074000 + step)

    def test_password_input_is_masked_and_never_saves_to_preferences(self):
        events = iter([(45, 1), (1, 1)])
        screen = SettingsScreens(
            self.display,
            -1,
            lambda _: [next(events)],
            self.module["text_character"],
            self.store,
        )
        with patch.object(screen, "text", wraps=screen.text) as text:
            self.assertIsNone(screen.input("Device password", "", secret=True))
        texts = [call.args[2] for call in text.call_args_list]
        self.assertIn("•▏", texts)
        self.assertNotIn("x▏", texts)
        self.assertFalse(self.store.path.exists())

    def test_download_progress_is_visible_and_esc_cancels(self):
        update = SimpleNamespace(
            state="downloading",
            updater=SimpleNamespace(release=("0.15.1",)),
            stage="download",
            received=512,
            total=1024,
            cancel=Mock(),
        )
        screen = SettingsScreens(
            self.display,
            -1,
            lambda _: [(1, 1)],
            self.module["text_character"],
            self.store,
        )
        screen.update(update)
        image = self.display.present.call_args.args[0]
        self.assertEqual(image.getpixel((50, 97)), (112, 237, 187))
        self.assertEqual(image.getpixel((280, 97)), (29, 45, 54))
        update.cancel.assert_called_once()

    def test_success_requires_explicit_restart(self):
        update = SimpleNamespace(
            state="done", stage="", updater=SimpleNamespace(release=("0.15.1",))
        )
        screen = SettingsScreens(
            self.display,
            -1,
            lambda _: [(28, 1)],
            self.module["text_character"],
            self.store,
        )
        with self.assertRaises(RestartApp):
            screen.update(update)
