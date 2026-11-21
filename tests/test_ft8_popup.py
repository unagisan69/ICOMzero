from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from PIL import Image, ImageFont
from ft8 import Message, History


class PopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        cls.display = SimpleNamespace(
            fonts={
                k: ImageFont.truetype(font, n)
                for k, n in [("small", 12), ("body", 14), ("title", 22)]
            }
        )

    def test_fault_is_centered_wrapped_card_not_top_banner(self):
        image = self.ui["ft8_frame"](
            self.display,
            History(),
            "K1ABC",
            True,
            "",
            0.5,
            fault="SWR telemetry lost -- TX stopped; check your radio connection",
        )
        self.assertNotEqual(image.getpixel((0, 0)), (138, 36, 36))
        self.assertEqual(image.getpixel((14, 85)), (255, 146, 146))
        self.assertEqual(image.getpixel((29, 85)), (61, 32, 40))

    def test_notifications_preserve_frame_size_and_center_card(self):
        image = self.ui["notice_popup"](
            self.display,
            Image.new("RGB", (320, 170), "black"),
            "ICOMzero",
            "Calling the next queued station on its receive period",
        )
        self.assertEqual(image.size, (320, 170))
        self.assertEqual(image.getpixel((14, 85)), (112, 237, 187))

    def test_pota_sota_have_green_background_and_country_column(self):
        h = History()
        h.update(
            [
                Message(100, 1500, "CQ POTA G4ABC IO91", -10),
                Message(101, 1500, "CQ SOTA JA1ABC PM95", -12),
            ],
            "K1ABC",
        )
        image = self.ui["ft8_frame"](self.display, h, "K1ABC", True, "", 0.5)
        for y in (2, 15):
            self.assertEqual(image.getpixel((318, y)), (23, 77, 54))
        self.assertNotEqual(
            image.crop((260, 1, 312, 13)).getextrema(), ((23, 23), (77, 77), (54, 54))
        )
