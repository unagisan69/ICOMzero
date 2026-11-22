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

    def test_radio_setup_saves_ipv4_from_stock_symbol_events(self):
        module = self.module
        values = ["", "50001", "", "", "Auto (Icom)", "60001", "User"]
        saved = []
        keys = [28, 2, 10, 3, 91, 11, 91, 3, 91, 2, 3, 4, 28, 1, 1]
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
                {
                    "Display": lambda _: Mock(),
                    "load": lambda: list(values),
                    "read_config": lambda: {},
                    "save": lambda data, *args: saved.append(list(data)),
                },
            ):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0], "192.0.2.123")
        self.assertEqual(saved[0][1:], values[1:])

    def test_s_opens_setup_while_receiving_and_stops_stream_before_edit(self):
        module = self.module
        values = ["192.0.2.1", "50001", "", "", "Auto (Icom)", "60001", "User"]
        saved = []
        with patch.dict(
            module["Receiver"].__init__.__globals__, {"read_config": lambda: {}}
        ):
            receiver = module["Receiver"]()
        receiver.thread = Mock()
        receiver.thread.is_alive.side_effect = lambda: not receiver.stop.is_set()

        def save(data):
            self.assertTrue(receiver.stop.is_set())
            receiver.thread.join.assert_called()
            saved.append(list(data))

        keys = [31, 28, 14, 3, 28, 1, 1]
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
                {
                    "Display": lambda _: Mock(),
                    "Receiver": lambda: receiver,
                    "load": lambda: list(values),
                    "save": save,
                },
            ):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(saved[0][0], "192.0.2.2")
