import struct
import unittest
from collections import OrderedDict
from civ import Civ, encode_frequency
from models import DEFAULT_PROFILE


class DigitalSetup(unittest.TestCase):
    def setUp(self):
        class Channel:
            ready = True
            seq = 0

            def __init__(self):
                self.sent = []
                self.history = OrderedDict()

            def send(self, *args, **kw):
                self.sent.append(kw["body"][9:-1])
                self.seq += 1

        self.channel = Channel()
        self.commands = []

        def take():
            result = self.commands[:]
            self.commands.clear()
            return result

        self.civ = Civ(
            self.channel,
            0xB6,
            lambda _: None,
            lambda *args: None,
            lambda: None,
            take,
            profile=DEFAULT_PROFILE,
            tx_enabled=True,
        )
        self.civ.started = self.civ.open = True
        self.response(b"\x03" + encode_frequency(14074000))
        self.response(b"\x04\x00\x01")
        self.response(self.civ.tx_source + b"\x00")
        self.response(b"\x1a\x06\x00\x00")
        for prefix in self.civ.tx_controls.values():
            self.response(prefix + b"\x00")

    def response(self, payload):
        frame = bytes.fromhex("fefee0b6") + payload + b"\xfd"
        self.civ.receive(
            bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
        )

    def test_receive_setup_uses_usb_data_after_original_settings_read(self):
        self.commands.append(("ft8_receive", None))
        for i in range(25):
            self.civ.tick(1 + i * 0.03)
        sent = self.channel.sent
        self.assertLess(sent.index(b"\x06\x01\x01"), sent.index(b"\x1a\x06\x01\x01"))
        self.assertNotIn(b"\x1c\x00\x01", sent)
        self.assertNotIn(self.civ.tx_source + b"\x05", sent)
        self.civ.close()
        self.assertIn(b"\x1a\x06\x00\x00", self.channel.sent)

    def test_tx_requires_lan_data_mod_usb_data_and_filter_readbacks(self):
        self.response(b"\x04\x01\x01")
        self.civ.prepared = True
        self.response(self.civ.tx_source + b"\x05")
        self.assertFalse(self.civ.tx_ready(14074000))
        self.response(b"\x1a\x06\x01\x02")
        self.assertFalse(self.civ.tx_ready(14074000))
        self.response(b"\x1a\x06\x01\x01")
        self.assertTrue(self.civ.tx_ready(14074000))
        self.assertEqual(self.civ.tx_source, bytes.fromhex("1a050085"))

    def test_reconnect_unkeys_even_before_first_ptt_readback(self):
        self.commands.append(("force_rx", None))
        self.civ.tick(1)
        self.assertIn(b"\x1c\x00\x00", self.channel.sent)
        self.assertNotIn(b"\x1c\x00\x01", self.channel.sent)

    def test_receive_setup_disables_and_restores_split_rit_xit(self):
        for name in self.civ.tx_controls:
            self.civ.controls[name] = 1
            self.civ.original_controls[name] = 1
        self.commands.append(("ft8_receive", None))
        for i in range(25):
            self.civ.tick(1 + i * 0.03)
        for prefix in self.civ.tx_controls.values():
            self.assertIn(prefix + b"\x00", self.channel.sent)
        self.civ.close()
        for prefix in self.civ.tx_controls.values():
            self.assertIn(prefix + b"\x01", self.channel.sent)
