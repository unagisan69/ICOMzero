import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from ft8 import History, Message, decode, decoder_path
from ft8qso import Sequencer, waveform
from ft8tx import Job, Pacer
from ft8log import DEFAULTS, Logbook, adif, save_settings, load_settings

SETTINGS = {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB"}


class HistoryTests(unittest.TestCase):
    def test_cq_and_own_call_only_with_stable_line_selection(self):
        h = History()
        rows = [Message(1, i, "CQ W1AW FN31", -10) for i in range(20)]
        unrelated = Message(1, 30, "K9XYZ W2XYZ -10", -10)
        own = Message(1, 31, "K1ABC W2XYZ -10", -10)
        h.update(rows + [unrelated, own], "K1ABC")
        self.assertNotIn(unrelated, h.messages)
        self.assertIn(own, h.messages)
        h.move(-1)
        self.assertEqual(h.current(), own)
        h.move(-1)
        self.assertEqual(h.current(), rows[-1])
        before = h.current()
        h.update(rows + [own, Message(2, 31, "CQ W3XYZ EN11", -20)], "K1ABC")
        self.assertEqual(h.current(), before)
        for _ in range(15):
            h.move(-1)
        self.assertIn(h.current(), h.visible())
        h.move(1)
        self.assertEqual(h.current(), rows[5])
        self.assertEqual(len(h.visible()), 11)


class Waveforms(unittest.TestCase):
    def test_standard_messages_round_trip(self):
        for text in (
            "CQ K1ABC FN42",
            "W1AW K1ABC FN42",
            "W1AW K1ABC -12",
            "W1AW K1ABC R-12",
            "W1AW K1ABC RRR",
            "W1AW K1ABC 73",
        ):
            pcm = waveform(text, 1500, 12000)
            capture = bytes(12000) + pcm + bytes(360000 - len(pcm) - 12000)
            self.assertIn(text, {text for _, _, text in decode(capture)}, text)

    def test_snr_tracks_awgn_in_2500_hz_bandwidth(self):
        raw = np.frombuffer(waveform("CQ K1ABC FN42", 1500, 12000), dtype="<i2") / 32768
        signal = np.zeros(180000)
        signal[6000 : 6000 + len(raw)] = raw
        rng = np.random.default_rng(123)
        for snr in (-15, -5, 0):
            sigma = np.sqrt(np.mean(raw**2) / 10 ** (snr / 10) * 6000 / 2500)
            capture = signal + rng.normal(0, sigma, len(signal))
            capture *= 0.9 / np.max(np.abs(capture))
            pcm = (capture * 32767).astype("<i2").tobytes()
            found = [
                item for item in decode(pcm, details=True) if item[2] == "CQ K1ABC FN42"
            ]
            self.assertTrue(found, snr)
            self.assertAlmostEqual(found[0][3], snr, delta=4)


class Pacing(unittest.TestCase):
    def make(self, start=100.5):
        self.keyed = False
        self.commands = []
        self.packets = []
        self.job = Job(bytes(1213440), start, 7074000)
        self.taken = False

        def take():
            if self.taken:
                return None
            self.taken = True
            return self.job

        def ptt(value):
            self.commands.append(value)
            self.keyed = value

        p = Pacer(
            take,
            ptt,
            self.packets.append,
            lambda dial: dial == 7074000,
            lambda: self.keyed,
            lambda _: None,
        )
        return p

    def test_audio_is_paced_then_unkeyed_and_completed(self):
        p = self.make()
        for i in range(1400):
            p.tick(100 + i * 0.01, 50 + i * 0.01)
        self.assertTrue(self.job.done.is_set())
        self.assertTrue(self.job.success)
        self.assertEqual(sum(map(len, self.packets)), 1213440)
        self.assertFalse(self.keyed)

    def test_cancel_before_period_never_keys(self):
        p = self.make()
        self.job.cancel.set()
        p.tick(100, 50)
        self.assertNotIn(True, self.commands)
        self.assertTrue(self.job.done.is_set())

    def test_clock_jump_and_audio_stall_unkey(self):
        for jump in (True, False):
            p = self.make()
            p.tick(100.4, 50.4)
            p.tick(100.5, 50.5)
            p.tick(102, 50.6 if jump else 52)
            self.assertFalse(self.keyed)
            self.assertFalse(self.job.success)
            self.assertTrue(self.job.done.is_set())

    def test_ptt_off_must_be_confirmed_before_success(self):
        p = self.make()
        p.tick(100.4, 50.4)
        p.ptt = lambda value: self.commands.append(value)
        p.end(True, wall=101, mono=51)
        self.assertFalse(self.job.done.is_set())
        p.tick(101.2, 51.2)
        self.assertFalse(self.job.done.is_set())
        self.keyed = False
        p.tick(101.3, 51.3)
        self.assertTrue(self.job.done.is_set())
        self.assertTrue(self.job.success)


class Contacts(unittest.TestCase):
    def setUp(self):
        self.completed = []
        self.seq = Sequencer(SETTINGS, self.completed.append)
        self.now = 1500.1

    def start_reply(self):
        with patch("time.time", return_value=self.now):
            self.seq.reply(Message(99, 1500, "CQ W1AW FN31", -12), 7074000)

    def sent(self, slot):
        pending = (self.seq.serial, self.seq.stage, self.seq.message, slot)
        self.seq.pending = pending
        self.seq.transmitted(pending, True)

    def received(self, slot, text):
        self.seq.receive(Message(slot, 1500, text, -12))

    def test_responder_finishes_after_73_transmitted_and_logs_once(self):
        self.start_reply()
        self.sent(100)
        self.received(101, "K1ABC W1AW -08")
        self.assertTrue(self.seq.message.endswith("R-12"))
        self.sent(102)
        self.received(103, "K1ABC W1AW RRR")
        self.assertTrue(self.seq.message.endswith(" 73"))
        self.assertFalse(self.completed)
        self.sent(104)
        self.assertEqual(len(self.completed), 1)
        self.assertEqual(self.completed[0]["received"], -8)
        self.received(105, "K1ABC W1AW 73")
        self.assertEqual(len(self.completed), 1)

    def test_caller_uses_rrr_and_waits_for_ack(self):
        with patch("time.time", return_value=self.now):
            self.seq.cq(7074000)
        self.sent(100)
        self.received(101, "K1ABC W1AW FN31")
        self.assertTrue(self.seq.message.endswith("-12"))
        self.sent(102)
        self.received(103, "K1ABC W1AW R-08")
        self.assertTrue(self.seq.message.endswith(" RRR"))
        self.assertNotIn("RR73", self.seq.message)
        self.received(103, "K1ABC W1AW 73")
        self.assertFalse(self.completed)
        self.sent(104)
        self.received(105, "K1ABC W1AW 73")
        self.assertEqual(len(self.completed), 1)

    def test_unrelated_old_and_untransmitted_contacts_never_log(self):
        self.start_reply()
        for slot, text in (
            (98, "K1ABC W1AW RRR"),
            (101, "K1ABC W2XYZ -10"),
            (103, "W9XYZ W1AW RRR"),
            (105, "K1ABC W1AW 73"),
        ):
            self.received(slot, text)
        self.assertEqual(self.completed, [])
        self.seq.stop()
        self.assertIsNone(self.seq.next(1500.3))

    def test_queue_uses_opposite_period_and_never_starts_mid_period(self):
        self.start_reply()
        self.assertIsNone(self.seq.next(1500.1))
        self.assertIsNone(self.seq.next(1515.3))
        self.assertIsNone(self.seq.next(1503))
        pending = self.seq.next(1500.3)
        self.assertEqual(pending[3], 100)


class Logging(unittest.TestCase):
    def test_local_log_and_per_service_outbox_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ):
            save_settings({**SETTINGS, "wavelog_enabled": True, "qrz_enabled": True})
            book = Logbook()
            book.closed.set()
            book.wake.set()
            book.thread.join(2)
            q = {
                "id": "test-id",
                "call": "W1AW",
                "own_call": "K1ABC",
                "own_grid": "FN42AB",
                "grid": "FN31",
                "dial": 7074000,
                "band": "40m",
                "start": 1500,
                "end": 1600,
                "sent": -12,
                "received": -8,
            }
            book.add(q)
            book.add(q)
            self.assertEqual(len(book.recent()), 1)
            self.assertEqual(
                sorted(book.summary()), ["qrz: 1 pending", "wavelog: 1 pending"]
            )
            record = adif(q)
            self.assertIn("<RST_SENT:3>-12", record)
            self.assertIn("<MY_GRIDSQUARE:6>FN42AB", record)
            self.assertEqual(
                (Path(root) / "icomzero/ft8.json").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(load_settings()["callsign"], "K1ABC")


class UserInterface(unittest.TestCase):
    def test_f_x_fn_scrolling_selects_lines_enter_replies_and_tab_opens_menu(self):
        from PIL import ImageFont
        from models import DEFAULT_PROFILE

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        receiver = module["Receiver"]()
        starts = []
        actions = []

        def start(_):
            starts.append(True)
            receiver.frequency = 7074000
            receiver.radio_state = {"mode": 1, "nr": False, "profile": DEFAULT_PROFILE}

        receiver.start = start
        receiver.busy = lambda: not receiver.stop.is_set()

        class Display:
            def __init__(self, _):
                font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                self.fonts = {
                    key: ImageFont.truetype(font, size)
                    for key, size in [("small", 12), ("body", 14), ("title", 22)]
                }

            def present(self, _):
                pass

        rows = [Message(100, i + 500, f"CQ W{i}AA FN31", -10) for i in range(20)]

        class Decoder:
            def __init__(self):
                self.feed = lambda _: None

            def snapshot(self):
                return rows, "Ready", 0.5

            def activate(self, *_, **__):
                pass

            def close(self):
                pass

        class Book:
            status = "Local log ready"

            def close(self):
                pass

        class Station:
            clock_status = "Clock synchronized"
            status = "Receive only"

            def __init__(self, *_):
                pass

            def action(self, *args, **kwargs):
                actions.append((args, kwargs))

            def close(self):
                pass

        events = []

        def key(code, value=1):
            events.append(module["EVENT"].pack(0, 0, 1, code, value))

        for code in (33, 33, 45):
            key(code)
            key(code, 0)
        key(389)
        key(33)
        key(33, 0)
        key(389, 0)
        key(389)
        key(45)
        key(45, 0)
        key(389, 0)
        key(482)
        key(482, 0)  # Linux KEY_FN_F is also up.
        key(28)  # Answers row 18, never stops reception.
        key(15)
        key(45)
        key(28)  # Tab -> Call CQ.
        key(1)
        read, write = os.pipe()
        try:
            os.write(write, b"".join(events))
            with patch.dict(
                module["ft8_ui"].__globals__,
                {
                    "Receiver": lambda: receiver,
                    "Display": Display,
                    "load": lambda: ["192.0.2.1", "50001", "test", "test"],
                },
            ), patch("ft8.Decoder", Decoder), patch("ft8log.Logbook", Book), patch(
                "ft8log.load_settings", return_value=SETTINGS
            ), patch(
                "ft8station.Station", Station
            ):
                module["ft8_ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.assertEqual(len(starts), 1)
        reply = next(args for args, _ in actions if args[0] == "reply")
        self.assertEqual(reply[1], rows[18])
        self.assertTrue(any(args[0] == "cq" for args, _ in actions))


class ClockInterlock(unittest.TestCase):
    def test_unsynchronized_clock_blocks_transmit(self):
        import ft8station
        from unittest.mock import Mock

        receiver = Mock()
        receiver.radio_state = {"profile": {"transmit": True}}
        decoder = Mock()
        decoder.snapshot.return_value = ([], "", 0)
        with patch(
            "ft8station.clock_health", return_value=(False, "Clock not synchronized")
        ):
            station = ft8station.Station(receiver, decoder, Mock())
            station.action("cq")
            end = time.monotonic() + 2
            while time.monotonic() < end and station.status != "Clock not synchronized":
                time.sleep(0.01)
            station.close()
        receiver.queue_tx.assert_not_called()
        self.assertFalse(station.active)


class IcomTransmit(unittest.TestCase):
    def test_lan_source_readback_ptt_and_restoration(self):
        import struct
        from collections import OrderedDict
        from civ import Civ, encode_frequency
        from models import DEFAULT_PROFILE

        class Channel:
            ready = True

            def __init__(self):
                self.sent = []
                self.seq = 0
                self.history = OrderedDict()

            def send(self, *_, **kwargs):
                self.sent.append(kwargs["body"][9:-1])
                self.history[self.seq] = kwargs["body"]
                self.seq += 1

        channel = Channel()
        commands = []
        civ = Civ(
            channel,
            0xB6,
            lambda _: None,
            lambda *_: None,
            lambda: None,
            lambda: commands[:],
            profile=DEFAULT_PROFILE,
            tx_enabled=True,
        )

        civ.started = True

        def response(payload):
            frame = bytes.fromhex("fefee0b6") + payload + b"\xfd"
            civ.receive(
                bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
            )

        response(b"\x03" + encode_frequency(7074000))
        response(b"\x04\x01\x01")
        response(bytes.fromhex(DEFAULT_PROFILE["tx_source"]) + b"\x00")
        response(b"\x1a\x06\x01\x02")
        for prefix in DEFAULT_PROFILE.get("tx_controls", {}).values():
            response(bytes.fromhex(prefix) + b"\x01")
        commands.append(("ft8_prepare", None))
        civ.tick(1)
        commands.clear()
        for i in range(15):
            civ.tick(1.03 + i * 0.03)
        self.assertFalse(civ.tx_ready(7074000))
        response(
            bytes.fromhex(DEFAULT_PROFILE["tx_source"])
            + bytes((DEFAULT_PROFILE["tx_lan"],))
        )
        response(b"\x1a\x06\x00\x01")
        self.assertFalse(civ.tx_ready(7074000))
        for prefix in DEFAULT_PROFILE.get("tx_controls", {}).values():
            response(bytes.fromhex(prefix) + b"\x00")
        self.assertTrue(civ.tx_ready(7074000))
        civ.ptt(True)
        civ.tick(2)
        self.assertNotIn(
            b"\x1c\x00\x01", [data[9:-1] for data in channel.history.values()]
        )
        response(b"\x1c\x00\x01")
        self.assertTrue(civ.ptt_state)
        response(b"\x1c\x00\x00")
        civ.close()
        self.assertIn(b"\x1c\x00\x00", channel.sent)
        self.assertIn(
            bytes.fromhex(DEFAULT_PROFILE["tx_source"]) + b"\x00", channel.sent
        )
        self.assertIn(b"\x1a\x06\x01\x02", channel.sent)


class UploadOutbox(unittest.TestCase):
    def test_services_retry_independently_and_never_overwrite_qrz(self):
        import ft8log

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ):
            save_settings(
                {
                    **SETTINGS,
                    "wavelog_enabled": True,
                    "wavelog_url": "https://example.test/index.php",
                    "wavelog_station": "1",
                    "qrz_enabled": True,
                }
            )
            calls = []
            qrz_allowed = False

            def request(url, data, form=False):
                calls.append((url, data, form))
                if "qrz.com" in url:
                    return {"RESULT": "OK" if qrz_allowed else "AUTH"}
                return {"status": "created", "adif_errors": 0}

            with patch("ft8log.request", side_effect=request):
                book = Logbook()
                q = {
                    "id": "test-qso",
                    "call": "W1AW",
                    "own_call": "K1ABC",
                    "own_grid": "FN42AB",
                    "grid": "FN31",
                    "dial": 7074000,
                    "band": "40m",
                    "start": 1500,
                    "end": 1600,
                    "sent": -12,
                    "received": -8,
                }
                book.add(q)
                end = time.monotonic() + 2
                while time.monotonic() < end and len(calls) < 2:
                    time.sleep(0.01)
                self.assertIn("wavelog: 1 sent", book.summary())
                self.assertIn("qrz: 1 pending", book.summary())
                qrz_allowed = True
                book.sync()
                end = time.monotonic() + 2
                while time.monotonic() < end and "qrz: 1 sent" not in book.summary():
                    time.sleep(0.01)
                book.close()
            self.assertEqual(
                len([url for url, _, _ in calls if "example.test" in url]), 1
            )
            for url, data, form in calls:
                if "qrz.com" in url:
                    self.assertEqual(data["ACTION"], "INSERT")
                    self.assertNotIn("OPTION", data)
                else:
                    self.assertEqual(data["station_profile_id"], "1")
                    self.assertEqual(data["type"], "adif")


class KenwoodTransmit(unittest.TestCase):
    def test_kns_source_ptt_readback_and_source_restoration(self):
        from kenwood import Cat

        sent = []
        cat = Cat(
            sent.append,
            "user",
            "password",
            False,
            lambda _: None,
            lambda *_: None,
            lambda *_: None,
            True,
        )
        cat.receive("FA00007074000")
        cat.receive("OM02")
        cat.receive("MS000")
        cat.receive("MS102")
        for prefix in ("TB", "RT", "XT"):
            cat.receive(prefix + "1")
        cat.action("ft8_prepare", None)
        cat.tx_poll()
        self.assertIn("MS003", sent)
        self.assertIn("MS103", sent)
        self.assertFalse(cat.tx_ready(7074000))
        cat.receive("MS003")
        cat.receive("MS103")
        self.assertFalse(cat.tx_ready(7074000))
        for prefix in ("TB", "RT", "XT"):
            cat.receive(prefix + "0")
        self.assertTrue(cat.tx_ready(7074000))
        cat.ptt(True)
        self.assertIn("TX0", sent)
        cat.receive("IF" + "0" * 26 + "1" + "0" * 8)
        self.assertTrue(cat.ptt_state)
        cat.close()
        self.assertIn("RX", sent)
        self.assertIn("MS000", sent)
        self.assertIn("MS102", sent)


class BandChangeInterlock(unittest.TestCase):
    def test_frequency_waits_for_ptt_off_readback(self):
        import struct
        from collections import OrderedDict, deque
        from civ import Civ, encode_frequency

        class Channel:
            ready = True

            def __init__(self):
                self.sent = []
                self.seq = 0
                self.history = OrderedDict()

            def send(self, *_, **kwargs):
                self.sent.append(kwargs["body"][9:-1])
                self.history[self.seq] = kwargs["body"]
                self.seq += 1

        channel = Channel()
        targets = deque([10136000])
        civ = Civ(
            channel,
            0xB6,
            lambda _: None,
            lambda *_: None,
            lambda: targets.popleft() if targets else None,
            tx_enabled=True,
        )
        civ.open = True
        civ.ptt_state = True
        civ.frequency = 7074000
        civ.ptt(False)
        civ.tick(1)
        self.assertEqual(channel.sent[-1], b"\x1c\x00\x00")
        civ.tick(1.03)
        self.assertEqual(channel.sent[-1], b"\x1c\x00")
        self.assertFalse(any(payload.startswith(b"\x05") for payload in channel.sent))
        frame = bytes.fromhex("fefee0b61c0000fd")
        civ.receive(
            bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
        )
        civ.tick(1.06)
        self.assertEqual(channel.sent[-1], b"\x05" + encode_frequency(10136000))


class ClockOffset(unittest.TestCase):
    def test_large_ntp_offset_blocks_transmit(self):
        from ft8station import clock_health

        for offset, expected in (
            ("+300ms", False),
            ("+2.4ms", True),
            ("-500ms", False),
        ):
            responses = [
                subprocess.CompletedProcess([], 0, b"yes\n"),
                subprocess.CompletedProcess([], 0, ("Offset: " + offset).encode()),
            ]
            with patch("subprocess.run", side_effect=responses):
                ok, status = clock_health()
            self.assertEqual(ok, expected, status)


class CivStartup(unittest.TestCase):
    def test_unsolicited_scope_cannot_replace_control_open_handshake(self):
        import struct
        from civ import Civ, encode_frequency

        class Channel:
            ready = True

            def __init__(self):
                self.sent = []

            def send(self, *_, **kwargs):
                self.sent.append(kwargs["body"])

        channel = Channel()
        civ = Civ(channel, 0xB6, lambda _: None, lambda *_: None, lambda: None)

        def response(destination, payload):
            frame = bytes((0xFE, 0xFE, destination, 0xB6)) + payload + b"\xfd"
            civ.receive(
                bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
            )

        response(0xE1, b"\x27\x00\x00")
        self.assertFalse(civ.open)
        civ.tick(1)
        self.assertTrue(civ.started)
        self.assertFalse(civ.open)
        self.assertEqual(channel.sent[0][0], 0xC0)
        civ.tick(1.04)
        self.assertEqual(channel.sent[-1][9:-1], b"\x03")
        response(0xE0, b"\x03" + encode_frequency(7074000))
        self.assertTrue(civ.open)


class TransportWaveform(unittest.TestCase):
    def test_48khz_waveform_and_configured_reply_frequency(self):
        pcm = waveform("CQ K1ABC FN42", 1500)
        samples = np.frombuffer(pcm, dtype="<i2")[::4]
        capture = np.concatenate(
            (
                np.zeros(6000, dtype="<i2"),
                samples,
                np.zeros(180000 - 6000 - len(samples), dtype="<i2"),
            )
        )
        self.assertIn(
            "CQ K1ABC FN42",
            {text for _, _, text in decode(capture.astype("<i2").tobytes())},
        )
        sequence = Sequencer(SETTINGS, lambda _: None)
        now = time.time()
        message = Message(int(now // 15) - 1, 300, "CQ W1AW FN31", -10)
        sequence.reply(message, 7074000)
        self.assertEqual(sequence.hz, 1500)


class EarlyDecode(unittest.TestCase):
    def test_full_pass_adds_late_signals_without_moving_early_selection(self):
        from ft8 import Decoder

        answers = [
            [(0.5, 1500, "CQ W1AW FN31", -10)],
            [(0.5, 1503, "CQ W1AW FN31", -9), (0.7, 2000, "CQ W2AW FN32", -20)],
        ]
        with patch("ft8.decode", side_effect=answers):
            decoder = Decoder()
            decoder.activate()
            generation = decoder.generation
            try:
                decoder.jobs.put((generation, 100, b"early"))
                end = time.monotonic() + 2
                while time.monotonic() < end and len(decoder.snapshot()[0]) < 1:
                    time.sleep(0.01)
                first = decoder.snapshot()[0][0]
                decoder.jobs.put((generation, 100, b"full"))
                end = time.monotonic() + 2
                while time.monotonic() < end and len(decoder.snapshot()[0]) < 2:
                    time.sleep(0.01)
                rows = decoder.snapshot()[0]
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0], first)
                self.assertEqual(decoder.last_decoded_slot, 100)
            finally:
                decoder.close()
