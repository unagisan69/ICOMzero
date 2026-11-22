"""Generated receive audio only; these tests never key a transmitter."""

import time
import unittest
from unittest.mock import patch
import numpy as np
from cw import Core, Decoder, Scrollback

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


def hand_recording(
    text="CQ TEST K1ABC 599",
    wpm=20,
    gap_scale=1.0,
    weight=0.0,
    jitter=0.0,
    hz=600,
    seed=41,
    letter_units=3,
    word_units=7,
):
    rng = np.random.default_rng(seed)
    rate = 48000
    unit = 1.2 / wpm
    parts = [np.zeros(int(0.2 * rate))]

    def silence(units):
        duration = (
            max(0.1, units * gap_scale - weight)
            * unit
            * rng.uniform(1 - jitter, 1 + jitter)
        )
        parts.append(np.zeros(round(duration * rate)))

    for wi, word in enumerate(text.split()):
        if wi:
            silence(word_units - letter_units)
        for letter in word:
            for index, symbol in enumerate(CODE[letter]):
                if index:
                    silence(1)
                duration = (
                    ((1 if symbol == "." else 3) + weight)
                    * unit
                    * rng.uniform(1 - jitter, 1 + jitter)
                )
                n = round(duration * rate)
                envelope = np.ones(n)
                ramp = min(96, n // 4)
                envelope[:ramp] = np.linspace(0, 1, ramp)
                envelope[-ramp:] = np.linspace(1, 0, ramp)
                parts.append(
                    0.35 * np.sin(2 * np.pi * hz * np.arange(n) / rate) * envelope
                )
            silence(letter_units)
    parts.append(np.zeros(round(1.7 * rate)))
    x = np.concatenate(parts)
    x += rng.normal(0, 0.015, len(x))
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


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

    def test_ssb_audio_high_pitch_with_a_second_tone(self):
        target = np.frombuffer(
            recording(wpm=20, hz=2000, noise=0.025), dtype="<i2"
        ).astype(float)
        harmonic = (
            np.frombuffer(recording(wpm=20, hz=1000, noise=0), dtype="<i2").astype(
                float
            )
            * 0.6
        )
        pcm = np.clip(target + harmonic, -32767, 32767).astype("<i2").tobytes()
        state = self.decode(pcm)
        self.assertEqual(state["text"].strip(), "CQ TEST K1ABC 599")
        self.assertLess(abs(state["pitch"] - 2000), 55)

    def test_manual_tone_ignores_a_louder_carrier_elsewhere(self):
        pcm = (
            np.frombuffer(recording(wpm=20, hz=2000, noise=0.025), dtype="<i2").astype(
                float
            )
            / 32768
        )
        pcm += 0.5 * np.sin(2 * np.pi * 1000 * np.arange(len(pcm)) / 48000)
        core = Core(tone=2000)
        data = (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
        for i in range(0, len(data), 1920):
            core.feed(data[i : i + 1920])
        self.assertEqual(core.snapshot()["text"].strip(), "CQ TEST K1ABC 599")
        self.assertEqual(core.snapshot()["pitch"], 2000)

    def test_weighted_marks_and_compressed_uneven_spacing(self):
        for options in (
            {"gap_scale": 0.65, "jitter": 0.12},
            {"weight": 0.35, "jitter": 0.12},
            {"weight": -0.2, "jitter": 0.1},
        ):
            with self.subTest(options=options):
                self.assertEqual(
                    self.decode(hand_recording(**options))["text"].strip(),
                    "CQ TEST K1ABC 599",
                )

    def test_stretched_letter_spacing_preserves_words_and_callsign(self):
        pcm = hand_recording(letter_units=6, word_units=14, jitter=0.04)
        self.assertEqual(self.decode(pcm)["text"].strip(), "CQ TEST K1ABC 599")

    def test_fading_does_not_split_dashes_or_erase_dots(self):
        x = np.frombuffer(recording(hz=600), dtype="<i2").astype(float) / 32768
        # About 18 dB of fading, independently of the keying pattern.
        fade = 0.45 + 0.35 * np.sin(2 * np.pi * np.arange(len(x)) / 48000 / 0.9)
        pcm = (np.clip(x * fade, -1, 1) * 32767).astype("<i2").tobytes()
        self.assertEqual(self.decode(pcm)["text"].strip(), "CQ TEST K1ABC 599")

    def test_nearby_carrier_does_not_steal_the_cw_tone(self):
        x = np.frombuffer(recording(hz=600), dtype="<i2").astype(float) / 32768
        x += 0.2 * np.sin(2 * np.pi * 850 * np.arange(len(x)) / 48000)
        state = self.decode((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        self.assertEqual(state["text"].strip(), "CQ TEST K1ABC 599")
        self.assertLess(abs(state["pitch"] - 600), 10)

    def test_manual_tone_finds_carrier_between_selector_steps(self):
        core = Core(tone=700)
        core.feed(recording(hz=747))
        self.assertEqual(core.snapshot()["text"].strip(), "CQ TEST K1ABC 599")
        self.assertLess(abs(core.snapshot()["pitch"] - 747), 10)

    def test_new_tone_learns_its_own_speed(self):
        state = self.decode(
            recording(wpm=12, hz=600)
            + bytes(48000 * 2 * 3)
            + recording(wpm=30, hz=1200)
        )
        self.assertEqual(state["text"].strip(), "CQ TEST K1ABC 599\nCQ TEST K1ABC 599")
        self.assertLess(abs(state["wpm"] - 30), 4)

    def test_receiver_noise_after_a_station_stops_adds_no_text(self):
        for bandwidth in (250, 500):
            chunks = [recording(hz=600)]
            for seed in (0, 2, 315):
                rng = np.random.default_rng(seed)
                n = 48000 * 5
                spectrum = np.fft.rfft(rng.normal(0, 1, n))
                hz = np.fft.rfftfreq(n, 1 / 48000)
                spectrum[(hz < 600 - bandwidth / 2) | (hz > 600 + bandwidth / 2)] = 0
                noise = np.fft.irfft(spectrum, n)
                noise = noise / np.std(noise) * 0.05
                chunks.append((np.clip(noise, -1, 1) * 32767).astype("<i2").tobytes())
            with self.subTest(bandwidth=bandwidth):
                self.assertEqual(
                    self.decode(b"".join(chunks))["text"].strip(), "CQ TEST K1ABC 599"
                )

    def test_filtered_receiver_noise_does_not_decode_as_letters(self):
        for bandwidth in (250, 500):
            n = 48000 * 8
            rng = np.random.default_rng(315)
            spectrum = np.fft.rfft(rng.normal(0, 1, n))
            hz = np.fft.rfftfreq(n, 1 / 48000)
            spectrum[(hz < 600 - bandwidth / 2) | (hz > 600 + bandwidth / 2)] = 0
            noise = np.fft.irfft(spectrum, n)
            noise = noise / np.std(noise) * 0.05
            pcm = (np.clip(noise, -1, 1) * 32767).astype("<i2").tobytes()
            self.assertEqual(self.decode(pcm)["text"].strip(), "", bandwidth)

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

    def test_short_network_burst_does_not_overrun_audio_queue(self):
        with patch("cw.threading.Thread.start"):
            decoder = Decoder()
            decoder.configure(True, 1)
            for _ in range(24):
                decoder.feed(bytes(960))
            self.assertEqual(decoder.dropped, 0)
            self.assertEqual(decoder.jobs.qsize(), 24)
        decoder.stop.set()

    def test_late_contiguous_audio_keeps_timing_and_actual_gap_resets_it(self):
        class Probe:
            def __init__(self, rate=48000, text="", tone=None, offset=0):
                self.text = text
                self.timing = self

            def feed(self, pcm):
                self.text += pcm.decode()

            def snapshot(self):
                return {"text": self.text}

        def wait_for(decoder, text):
            end = time.monotonic() + 2
            while time.monotonic() < end:
                if decoder.snapshot()["text"] == text:
                    return
                time.sleep(0.005)
            self.fail(decoder.snapshot())

        with patch("cw.Core", Probe):
            decoder = Decoder()
            decoder.configure(True, 1)
            try:
                decoder.feed(b"first")
                wait_for(decoder, "first")
                # The captured radio stream had 184 ms of jitter with no missing sequence.
                time.sleep(0.2)
                decoder.feed(b"second")
                wait_for(decoder, "firstsecond")
                decoder.discontinuity()
                decoder.feed(b"next")
                wait_for(decoder, "firstsecond\nnext")
            finally:
                decoder.close()

    def test_audio_overrun_invalidates_queued_morse_timing(self):
        with patch("cw.threading.Thread.start"):
            decoder = Decoder()
            decoder.configure(True, 1)
            generation = decoder.generation
            for _ in range(65):
                decoder.feed(bytes(960))
            self.assertGreater(decoder.generation, generation)
            self.assertEqual(decoder.dropped, 1)
            self.assertLessEqual(decoder.jobs.qsize(), 64)
        decoder.stop.set()


class CWScrollback(unittest.TestCase):
    def test_scroll_stays_put_and_bottom_reenables_follow(self):
        history = Scrollback(columns=5, rows=2)
        text = "one two three four five six"
        history.update(text, 1)
        self.assertFalse(history.view()["browsing"])
        history.scroll(-1)
        old = history.view()["lines"]
        history.update(text + " seven eight", 1)
        self.assertEqual(history.view()["lines"], old)
        for _ in range(20):
            history.scroll(1)
        self.assertFalse(history.view()["browsing"])
        history.update(text + " seven eight nine", 1)
        self.assertTrue(history.view()["lines"][-1].endswith("nine▏"))

    def test_resync_adds_line_break_instead_of_clearing(self):
        history = Scrollback(columns=30, rows=3)
        history.update("OLD MESSAGE", 1)
        history.update("NEW MESSAGE", 2)
        self.assertEqual(history.text, "OLD MESSAGE\nNEW MESSAGE")
        self.assertEqual(history.view()["lines"], ["OLD MESSAGE", "NEW MESSAGE▏"])

    def test_provisional_word_is_replaced_without_duplicates(self):
        history = Scrollback()
        history.update("CQ TE", 1)
        history.update("CQ TEST", 1)
        self.assertEqual(history.text, "CQ TEST")

    def test_bounded_history_keeps_reading_anchor_when_head_expires(self):
        history = Scrollback(columns=4, rows=2, limit=20)
        history.update("aaaa bbbb cccc dddd", 1)
        history.scroll(-2)
        history.update("bbbb cccc dddd eeee", 1, offset=5)
        self.assertLessEqual(len(history.text), 20)
        self.assertTrue(history.view()["browsing"])
        self.assertEqual(history.view()["lines"], ["bbbb", "cccc"])


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

    def test_w_toggles_decoding_without_changing_mode_or_frequency(self):
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
        rx.frequency = 7054000
        rx.radio_state = {"mode": 0}
        rx.thread = Mock()
        rx.thread.is_alive.return_value = True
        rx.command = Mock()
        decoder = Mock()
        decoder.snapshot.return_value = {
            "text": "",
            "pitch": None,
            "wpm": None,
            "key": False,
            "status": "Listening for CW",
        }
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
            ), patch("cw.Decoder", return_value=decoder):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        rx.command.assert_not_called()
        self.assertEqual(rx.frequency, 7054000)
        self.assertEqual(rx.radio_state["mode"], 0)
        self.assertIn(True, [call.args[0] for call in decoder.configure.call_args_list])
        self.assertFalse(decoder.configure.call_args.args[0])

    def test_f_x_and_fn_arrows_scroll_without_tuning(self):
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
        rx.frequency = 7029000
        rx.radio_state = {"mode": 0}
        rx.thread = Mock()
        rx.thread.is_alive.return_value = True
        rx.tune = Mock()
        rx.command = Mock()
        decoder = Mock()
        decoder.snapshot.return_value = {
            "text": "CQ TEST",
            "pitch": 600,
            "wpm": 20,
            "key": False,
        }
        moves = []

        class Probe(Scrollback):
            def scroll(self, direction):
                moves.append(direction)
                super().scroll(direction)

        read, write = os.pipe()
        try:
            events = (
                (17, 1),
                (33, 1),
                (33, 2),
                (45, 1),
                (389, 1),
                (103, 1),
                (108, 1),
                (389, 0),
                (1, 1),
            )
            os.write(
                write,
                b"".join(
                    module["EVENT"].pack(0, 0, 1, key, value) for key, value in events
                ),
            )
            with patch.dict(
                module["ui"].__globals__,
                {
                    "Display": lambda _: Mock(),
                    "Receiver": lambda: rx,
                    "load": lambda: ["192.0.2.1", "50001", "test", "test"],
                },
            ), patch("cw.Decoder", return_value=decoder), patch("cw.Scrollback", Probe):
                module["ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(moves, [-1, 1, -1, 1])
        rx.tune.assert_not_called()
        rx.command.assert_not_called()

    def test_holding_f_repeats_scroll_until_release(self):
        import os, runpy, threading
        from pathlib import Path
        from unittest.mock import Mock

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        with patch.dict(
            module["Receiver"].__init__.__globals__, {"read_config": lambda: {}}
        ):
            rx = module["Receiver"]()
        rx.thread = Mock()
        rx.thread.is_alive.return_value = True
        rx.frequency = 7029000
        rx.radio_state = {"mode": 0}
        decoder = Mock()
        decoder.snapshot.return_value = {
            "text": "CQ TEST " * 100,
            "pitch": 600,
            "wpm": 20,
            "key": False,
        }
        moves = []

        class Probe(Scrollback):
            def scroll(self, direction):
                moves.append(direction)
                super().scroll(direction)

        read, write = os.pipe()

        def keys():
            for key in (17, 33):
                os.write(write, module["EVENT"].pack(0, 0, 1, key, 1))
            time.sleep(0.55)
            os.write(
                write,
                module["EVENT"].pack(0, 0, 1, 33, 0)
                + module["EVENT"].pack(0, 0, 1, 1, 1),
            )

        thread = threading.Thread(target=keys)
        try:
            with patch.dict(
                module["ui"].__globals__,
                {
                    "Display": lambda _: Mock(),
                    "Receiver": lambda: rx,
                    "load": lambda: ["192.0.2.1", "50001", "test", "test"],
                },
            ), patch("cw.Decoder", return_value=decoder), patch("cw.Scrollback", Probe):
                thread.start()
                module["ui"](-1, read)
        finally:
            thread.join(2)
            os.close(read)
            os.close(write)
        self.assertGreaterEqual(len(moves), 2)
        self.assertTrue(all(move == -1 for move in moves))

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
