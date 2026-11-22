"""Generated receive audio only; these tests never key a transmitter."""

import time
import unittest
from unittest.mock import patch
import numpy as np
from cw import Core, Decoder

# Independent timing fixture for a short conventional CQ/report message.
CODE = {
    "C": "-.-.",
    "Q": "--.-",
    "T": "-",
    "E": ".",
    "S": "...",
    "K": "-.-",
    "1": ".----",
    "A": ".-",
    "B": "-...",
    "5": ".....",
    "9": "----.",
}


def recording(text="CQ TEST K1ABC 599", wpm=20, hz=600, noise=0.025, rate=48000):
    rng = np.random.default_rng(87)
    unit = 1.2 / wpm
    parts = [np.zeros(int(0.2 * rate))]

    def silence(units):
        parts.append(np.zeros(round(units * unit * rate)))

    for word_index, word in enumerate(text.split()):
        if word_index:
            silence(4)  # Each preceding letter already ends with 3 units.
        for letter in word:
            code = CODE[letter]
            for index, symbol in enumerate(code):
                if index:
                    silence(1)
                n = round((1 if symbol == "." else 3) * unit * rate)
                envelope = np.ones(n)
                ramp = min(int(0.002 * rate), n // 4)
                envelope[:ramp] = np.linspace(0, 1, ramp)
                envelope[-ramp:] = np.linspace(1, 0, ramp)
                parts.append(
                    0.35
                    * rng.uniform(0.75, 1.25)
                    * np.sin(2 * np.pi * hz * np.arange(n) / rate)
                    * envelope
                )
            silence(3)
    parts.append(np.zeros(int(1.7 * rate)))
    samples = np.concatenate(parts)
    samples += rng.normal(0, noise, len(samples))
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


class CW(unittest.TestCase):
    def decode(self, pcm, rate=48000):
        core = Core(rate)
        # Packet fragmentation is independent of tone edges or detector windows.
        sizes = (1920, 480, 1538, 960, 2400)
        pos = i = 0
        while pos < len(pcm):
            size = sizes[i % len(sizes)]
            core.feed(pcm[pos : pos + size])
            pos += size
            i += 1
        return core.snapshot()

    def test_clear_and_noisy_cw_at_different_speeds_and_pitches(self):
        for wpm, hz in ((8, 475), (12, 600), (20, 725), (30, 900), (35, 550)):
            with self.subTest(wpm=wpm, hz=hz):
                state = self.decode(recording(wpm=wpm, hz=hz))
                self.assertEqual(state["text"].strip(), "CQ TEST K1ABC 599", state)
                self.assertLess(abs(state["wpm"] - wpm), 4)
                self.assertLess(abs(state["pitch"] - hz), 55)

    def test_twelve_khz_and_quiet_audio_decode(self):
        state = self.decode(recording(wpm=18, hz=650, noise=0.01, rate=12000), 12000)
        self.assertEqual(state["text"].strip(), "CQ TEST K1ABC 599")

    def test_noise_silence_and_continuous_tone_do_not_create_messages(self):
        rng = np.random.default_rng(2)
        for pcm in (
            bytes(48000 * 2 * 4),
            (rng.normal(0, 0.05, 48000 * 4) * 32767).astype("<i2").tobytes(),
            (0.3 * np.sin(2 * np.pi * 650 * np.arange(48000 * 4) / 48000) * 32767)
            .astype("<i2")
            .tobytes(),
        ):
            self.assertEqual(self.decode(pcm)["text"].strip(), "")

    def test_unkeyed_carrier_has_no_speed_estimate(self):
        pcm = (
            0.3 * np.sin(2 * np.pi * 600 * np.arange(48000 * 3) / 48000) * 32767
        ).astype("<i2").tobytes() + bytes(48000 * 4)
        self.assertIsNone(self.decode(pcm)["wpm"])

    def test_inactive_feed_and_context_change_cannot_leak_partial_letters(self):
        decoder = Decoder()
        try:
            decoder.feed(bytes(960))
            self.assertTrue(decoder.jobs.empty())
            decoder.configure(True, (1, 7025000, 3))
            generation = decoder.generation
            decoder.state["text"] = "OLD"
            decoder.configure(True, (1, 14025000, 3))
            self.assertGreater(decoder.generation, generation)
            self.assertEqual(decoder.snapshot()["text"], "")
            decoder.configure(False)
            decoder.feed(bytes(960))
            self.assertTrue(decoder.jobs.empty())
        finally:
            decoder.close()

    def test_audio_overrun_invalidates_queued_morse_timing(self):
        with patch("cw.threading.Thread.start"):
            decoder = Decoder()
            decoder.configure(True, 1)
            generation = decoder.generation
            for _ in range(17):
                decoder.feed(bytes(960))
            self.assertGreater(decoder.generation, generation)
            self.assertEqual(decoder.dropped, 1)
            self.assertLessEqual(decoder.jobs.qsize(), 16)
        decoder.stop.set()


class RadioCW(unittest.TestCase):
    def test_icom_cw_and_reverse_request_filter_two_then_readback(self):
        from civ import Civ

        class Stream:
            ready = True

            def __init__(self):
                self.sent = []

            def send(self, *args, **kwargs):
                self.sent.append(kwargs["body"][9:-1])

        for mode in (3, 7):
            stream = Stream()
            commands = [("mode", mode)]
            civ = Civ(
                stream,
                0xB6,
                lambda _: None,
                lambda *args: None,
                lambda: None,
                lambda: commands.copy() if commands else (),
            )
            civ.open = True
            civ.tick(1)
            self.assertEqual(stream.sent[-1], bytes((6, mode, 2)))
            commands.clear()
            civ.tick(1.03)
            self.assertEqual(stream.sent[-1], b"\x04")

    def test_w_selects_cw_and_returns_to_previous_sideband(self):
        import os, runpy
        from pathlib import Path
        from unittest.mock import Mock

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        with patch.dict(
            module["Receiver"].__init__.__globals__, {"read_config": lambda: {}}
        ):
            rx = module["Receiver"]()
        rx.frequency = 7025000
        rx.radio_state = {"mode": 0}
        rx.thread = Mock()
        rx.thread.is_alive.return_value = True
        requests = []

        def command(action, value):
            requests.append((action, value))
            rx.radio_state[action] = value

        rx.command = command
        read, write = os.pipe()
        try:
            os.write(
                write,
                b"".join(module["EVENT"].pack(0, 0, 1, key, 1) for key in (17, 17, 1)),
            )
            with patch.dict(
                module["ui"].__globals__,
                {
                    "Display": lambda _: Mock(),
                    "Receiver": lambda: rx,
                    "load": lambda: ["192.0.2.1", "50001", "test", "test"],
                },
            ):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(requests, [("mode", 3), ("mode", 0)])

    def test_band_presets_and_band_change_keep_cw(self):
        import runpy
        from pathlib import Path
        from unittest.mock import Mock
        from models import DEFAULT_PROFILE, presets

        self.assertEqual(dict(presets(DEFAULT_PROFILE, cw=True))["20m"], 14025000)
        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        with patch.dict(
            module["Receiver"].__init__.__globals__, {"read_config": lambda: {}}
        ):
            rx = module["Receiver"]()
        rx.radio_state = {"mode": 7}
        rx.tune = Mock()
        rx.command = Mock()
        rx.change_band(14025000)
        rx.command.assert_called_once_with("mode", 7)
