import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch
from ft8tx import Pacer, Job
from ft8station import Station
from ft8log import DEFAULTS
import test_rf_protection as rf_tests
from models import DEFAULT_PROFILE


class PttTiming(unittest.TestCase):
    def test_delayed_confirmation_still_starts_audio_on_utc_deadline(self):
        jobs = [Job(bytes(96000), 100.5, 7074000)]
        job = jobs[0]
        commands = []
        packets = []
        keyed = [False]
        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            commands.append,
            lambda pcm: packets.append((now[0], pcm)),
            lambda _: True,
            lambda: keyed[0],
            lambda _: None,
        )
        now = [100.0]
        for i in range(51):
            now[0] = 100 + i * 0.01
            if i >= 30:
                keyed[0] = True
            p.tick(now[0], 50 + i * 0.01)
        self.assertEqual(commands[0], True)
        self.assertTrue(packets)
        self.assertAlmostEqual(packets[0][0], 100.5)
        self.assertTrue(job.started.is_set())

    def test_missing_confirmation_never_sends_audio(self):
        jobs = [Job(bytes(96000), 100.5, 7074000)]
        job = jobs[0]
        commands = []
        packets = []
        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            commands.append,
            packets.append,
            lambda _: True,
            lambda: False,
            lambda _: None,
        )
        for i in range(68):
            p.tick(100 + i * 0.01, 50 + i * 0.01)
        self.assertFalse(packets)
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.error, "PTT not confirmed")
        self.assertFalse(commands[-1])

    def test_return_to_rx_after_audio_starts_aborts_frame(self):
        jobs = [Job(bytes(96000), 100.5, 7074000)]
        job = jobs[0]
        keyed = [True]
        commands = []
        packets = []
        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            commands.append,
            packets.append,
            lambda _: True,
            lambda: keyed[0],
            lambda _: None,
        )
        p.tick(100, 50)
        p.tick(100.5, 50.5)
        before = len(packets)
        keyed[0] = False
        p.tick(100.51, 50.51)
        self.assertEqual(len(packets), before)
        self.assertTrue(job.done.is_set())
        self.assertEqual(job.error, "Radio returned to RX during TX")
        self.assertFalse(commands[-1])

    def test_rx_reply_does_not_acknowledge_requested_tx(self):
        civ, ch, _, _ = rf_tests.TransportSafety.make(self)
        civ.ptt(True)
        civ.tick(10)

        def response(state):
            frame = bytes.fromhex("fefee0b61c00") + bytes((state, 0xFD))
            civ.receive(
                bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
            )

        response(0)
        self.assertTrue(civ.ptt_pending)
        civ.tick(10.06)
        self.assertIn(bytes.fromhex("1c00"), ch.sent)
        response(1)
        self.assertFalse(civ.ptt_pending)
        self.assertTrue(civ.ptt_state)

    def test_waveform_is_built_before_the_transmit_period(self):
        receiver = Mock()
        receiver.frequency = 7074000
        receiver.stop = threading.Event()
        receiver.radio_state = {
            "profile": DEFAULT_PROFILE,
            "tx_ready": True,
            "ptt": False,
        }
        receiver.busy.return_value = True
        decoder = Mock()
        decoder.snapshot.return_value = ([], "", 0)
        decoder.last_decoded_slot = 99
        settings = {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42"}
        with patch("ft8station.clock_health", return_value=(True, "Synced")), patch(
            "ft8station.load_settings", return_value=settings
        ), patch("time.time", return_value=1502), patch(
            "ft8station.waveform", return_value=bytes(96000)
        ) as encoder:
            station = Station(receiver, decoder, Mock())
            try:
                station.action("cq")
                end = time.monotonic() + 2
                while not encoder.called and time.monotonic() < end:
                    time.sleep(0.01)
                encoder.assert_called_once()
                receiver.queue_tx.assert_not_called()
            finally:
                station.close()
