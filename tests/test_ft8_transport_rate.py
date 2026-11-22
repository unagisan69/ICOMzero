import queue
import threading
import unittest
from unittest.mock import patch
import numpy as np
from ft8 import Decoder, decode
from ft8qso import waveform
from ft8tx import Job, Pacer
import test_rf_protection as fixtures


class LowRateTransport(unittest.TestCase):
    def test_native_capture_preserves_full_slot_samples(self):
        with patch.object(threading.Thread, "start"):
            decoder = Decoder(rate=12000)
        decoder.activate()
        decoder.jobs = queue.Queue(maxsize=10)
        # Begin before a boundary, then capture one complete period unchanged.
        pcm = np.arange(240, dtype="<i2").tobytes()
        for i in range(1550):
            decoder.feed(pcm, now=149.02 + i * 0.02)
        captures = []
        while not decoder.jobs.empty():
            _, slot, data = decoder.jobs.get_nowait()
            if slot == 10 and len(data) >= 359800:
                captures.append(data)
        self.assertTrue(captures)
        self.assertLessEqual(abs(len(captures[0]) - 360000), 2)
        self.assertEqual(captures[0][:480], pcm)

    def test_twenty_ms_packets_preserve_duration_and_payload(self):
        pcm = bytes(range(240)) * 100
        job = Job(pcm, 100.5, 7074000)
        jobs, packets = [job], []
        pacer = Pacer(
            lambda: jobs.pop() if jobs else None,
            lambda _: None,
            packets.append,
            lambda _: True,
            lambda: True,
            lambda _: None,
            rate=12000,
            packet_ms=20,
            off_confirmed=lambda: True,
        )
        for i in range(1801):
            pacer.tick(100 + i * 0.001, 50 + i * 0.001)
        self.assertTrue(job.success)
        self.assertEqual(b"".join(packets), pcm)
        self.assertTrue(all(len(p) == 480 for p in packets))

    def test_meter_queries_are_staggered_and_every_half_second(self):
        civ, channel, _, _ = fixtures.TransportSafety.make(self)
        civ.ptt_state = True
        civ.next_poll = civ.tx_poll = civ.next_scope = 999
        sent = []
        for i in range(200):
            now = 10 + i * 0.01
            with patch("time.monotonic", return_value=now):
                civ.tick(now)
            while channel.sent:
                command = channel.sent.pop(0)
                if command in (
                    b"\x15\x12",
                    b"\x15\x13",
                    b"\x15\x11",
                    b"\x1c\x00",
                    b"\x1c\x01",
                ):
                    sent.append((now, command))
        self.assertGreaterEqual(len(sent), 18)
        self.assertLessEqual(len(sent), 20)
        self.assertTrue(all(b[0] - a[0] >= 0.099 for a, b in zip(sent, sent[1:])))
        for command in (b"\x15\x12", b"\x15\x13", b"\x15\x11"):
            times = [stamp for stamp, cmd in sent if cmd == command]
            self.assertGreaterEqual(len(times), 3)
            self.assertTrue(
                all(0.49 <= b - a <= 0.56 for a, b in zip(times, times[1:]))
            )

    def test_native_rate_encoder_is_decodable(self):
        pcm = waveform("CQ K4TTL EM85", 1500, rate=12000, level=50)
        self.assertEqual(len(pcm), 303360)
        recording = bytes(12000) + pcm + bytes(360000 - 12000 - len(pcm))
        messages = {text for _, _, text in decode(recording)}
        self.assertIn("CQ K4TTL EM85", messages)

    def test_radio_buffer_is_drained_before_ptt_off(self):
        job = Job(bytes(24000), 100.5, 7074000)
        jobs, commands = [job], []
        pacer = Pacer(
            lambda: jobs.pop() if jobs else None,
            commands.append,
            lambda _: None,
            lambda _: True,
            lambda: True,
            lambda _: None,
            rate=12000,
            packet_ms=20,
            drain_seconds=0.35,
            off_confirmed=lambda: True,
        )
        for i in range(1841):
            pacer.tick(100 + i * 0.001, 50 + i * 0.001)
        self.assertFalse(job.done.is_set())
        self.assertEqual(commands, [True])
        pacer.tick(101.86, 51.86)
        self.assertTrue(job.success)
        self.assertEqual(commands[-1], False)
