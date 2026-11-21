import queue
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
import numpy as np
from ft8 import Decoder, decode


class FT8(unittest.TestCase):
    def test_known_recording_decodes_real_messages_and_silence_does_not(self):
        root = Path(__file__).parent / "fixtures/ft8"
        with wave.open(str(root / "191111_110130.wav")) as wav:
            pcm = wav.readframes(wav.getnframes())
        expected = {
            "CQ TA6CQ KN70",
            "OH3NIV ZS6S -03",
            "CQ R7IW LN35",
            "CQ DX R6WA LN32",
            "TK4LS YC1MRF 73",
        }
        found = {text for _, _, text in decode(pcm)}
        self.assertGreaterEqual(len(found & expected), 4)
        self.assertTrue(found <= expected)
        self.assertEqual(decode(bytes(360000)), [])

    def test_capture_skips_partial_slot_and_resets_after_gap(self):
        with patch.object(threading.Thread, "start"):
            engine = Decoder()
        engine.activate()
        engine.jobs = queue.Queue(maxsize=10)
        pcm = np.zeros(480, dtype="<i2").tobytes()
        for i in range(4600):
            engine.feed(pcm, now=151.23 + i * 0.01)
        generation, slot, capture = engine.jobs.get_nowait()
        self.assertEqual(slot, 11)  # 165–180 UTC seconds; first partial slot skipped.
        self.assertEqual(len(capture), 336000)
        generation, slot, capture = engine.jobs.get_nowait()
        self.assertEqual(slot, 11)
        self.assertGreaterEqual(len(capture), 359800)
        self.assertLessEqual(len(capture), 360002)
        engine.feed(pcm, now=220)
        self.assertFalse(engine.complete)
        self.assertEqual(engine.parts, [])
        engine.activate(False)
        engine.feed(pcm, now=225)
        self.assertIsNone(engine.slot)

    def test_band_change_discards_inflight_decode(self):
        entered, release = threading.Event(), threading.Event()

        def delayed(_, **kwargs):
            entered.set()
            release.wait(2)
            return [(0, 1000, "CQ TEST AA00", -10)]

        with patch("ft8.decode", side_effect=delayed):
            engine = Decoder()
            engine.activate()
            engine.jobs.put((engine.generation, 10, b"pcm"))
            self.assertTrue(entered.wait(2))
            engine.activate()
            release.set()
            engine.close()
        self.assertEqual(engine.snapshot()[0], [])


class RadioModeShortcut(unittest.TestCase):
    def test_m_toggles_usb_lsb_without_changing_frequency(self):
        import os
        import runpy

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        receiver = module["Receiver"]()
        receiver.frequency = 7074000
        receiver.radio_state = {"mode": 0}
        commands = []

        def command(action, value=None):
            commands.append((action, value))
            receiver.radio_state["mode"] = value

        receiver.command = command

        class Display:
            def __init__(self, _):
                pass

            def render(self, *_):
                pass

        read, write = os.pipe()
        try:
            os.write(
                write,
                b"".join(module["EVENT"].pack(0, 0, 1, key, 1) for key in (50, 50, 1)),
            )
            with patch.dict(
                module["ui"].__globals__,
                {
                    "Receiver": lambda: receiver,
                    "Display": Display,
                    "load": lambda: ["192.0.2.1", "50001", "test", "test"],
                },
            ):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(commands, [("mode", 1), ("mode", 0)])
        self.assertEqual(receiver.frequency, 7074000)


class SilentReception(unittest.TestCase):
    def test_ft8_audio_reaches_decoder_without_opening_speaker(self):
        import runpy

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        raw = np.arange(480, dtype="<i2").tobytes()
        captured = []

        def listen(*args, **kwargs):
            args[6](raw)
            args[4].set()

        with patch.dict(module["Receiver"].run.__globals__, {"listen": listen}), patch(
            "subprocess.Popen"
        ) as player:
            receiver = module["Receiver"]()
            receiver.volume, receiver.muted = 35, False
            receiver.play_audio = False
            receiver.audio_observer = captured.append
            receiver.start(("192.0.2.1", 50001, "user", "password"))
            receiver.thread.join(2)
        player.assert_not_called()
        self.assertEqual(captured, [raw])
        self.assertEqual(receiver.packets, 1)
        self.assertEqual((receiver.volume, receiver.muted), (35, False))
        self.assertFalse(receiver.busy())
