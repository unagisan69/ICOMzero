from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from desktop_ui import DesktopApp


class NativeFocus(unittest.TestCase):
    def ui(self):
        ui = DesktopApp.__new__(DesktopApp)
        ui.closing = False
        ui.tk = SimpleNamespace(TclError=RuntimeError)
        ui.root = Mock()
        ui.root._w = "."
        ui.root.focus_get.side_effect = KeyError("popdown")
        ui.root.focus_displayof.side_effect = KeyError("popdown")
        ui.releases = {1: "release-id"}
        ui.held = {1: "ordinary"}
        return ui

    def test_native_popdown_focus_does_not_require_python_widget_lookup(self):
        ui = self.ui()
        ui.root.tk.call.return_value = ".!notebook.!frame.!combobox.popdown.f.l"
        self.assertTrue(ui.focus_path().endswith(".popdown.f.l"))
        ui.check_focus()
        self.assertEqual(ui.held, {1: "ordinary"})
        ui.root.focus_get.assert_not_called()
        ui.root.focus_displayof.assert_not_called()

    def test_external_focus_releases_held_keys(self):
        ui = self.ui()
        ui.root.tk.call.return_value = ""
        ui.check_focus()
        self.assertFalse(ui.held)
        self.assertFalse(ui.releases)
        ui.root.after_cancel.assert_called_once_with("release-id")

    def test_closing_does_not_query_destroyed_widgets(self):
        ui = self.ui()
        ui.closing = True
        self.assertEqual(ui.focus_path(), "")
        ui.check_focus()
        ui.root.tk.call.assert_not_called()


class CallsignRender(unittest.TestCase):
    def test_received_and_local_tx_callsigns_use_yellow(self):
        import runpy
        from pathlib import Path
        from PIL import ImageDraw, ImageFont
        from unittest.mock import patch
        from ft8 import History, Message

        app = runpy.run_path(str(Path(__file__).resolve().parents[1] / "app/icomzero"))
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        for text, local in (("K1ABC W1AW RR73", False), ("W1AW K1ABC RRR", True)):
            display = SimpleNamespace(fonts={"small": font})
            history = History()
            history.update([Message(100, 1500, text, -10, local)], "K1ABC")
            original = ImageDraw.ImageDraw.text
            with patch.object(
                ImageDraw.ImageDraw, "text", autospec=True, side_effect=original
            ) as draw:
                app["ft8_frame"](display, history, "K1ABC", True, "", 0.5)
            expected = ("TX " if local else "") + text
            calls = [c for c in draw.call_args_list if c.args[2] == expected]
            self.assertEqual(calls[0].kwargs["fill"], "#ffe45e")
