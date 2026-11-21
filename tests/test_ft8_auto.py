import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ft8 import Message
from ft8auto import AutoCQ, SessionLog
from ft8log import DEFAULTS
from ft8qso import Sequencer
from notifications import Sounds, tone


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=1500.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.events = []
        self.saved = []
        self.seq = Sequencer(
            {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB", "cq_period": "Even"},
            self.saved.append,
        )
        self.auto = AutoCQ(
            self.seq, lambda event, **data: self.events.append((event, data))
        )
        self.auto.start(7074000)

    def send(self, slot):
        self.now.return_value = slot * 15 + 0.05
        pending = self.auto.next()
        self.assertIsNotNone(pending)
        self.auto.transmitted(pending, True)
        return pending

    def receive(self, slot, text):
        self.now.return_value = (slot + 1) * 15 + 0.02
        self.auto.receive(Message(slot, 1450, text, -12))

    def test_four_cqs_wait_full_receive_then_stop(self):
        for slot in (100, 102, 104, 106):
            self.send(slot)
        self.assertTrue(self.auto.active)
        self.assertIsNone(self.auto.next(107 * 15 + 0.05))
        self.assertIsNone(self.auto.next(108 * 15 + 0.05))
        self.assertFalse(self.auto.active)
        self.assertTrue(self.auto.finished_no_response)
        self.assertEqual(self.seq.repeat_limit, 5)

    def test_last_receive_reply_gets_contact_attempts_then_returns_cq(self):
        for slot in (100, 102, 104, 106):
            self.send(slot)
        self.receive(107, "K1ABC W1AW FN31")
        self.assertEqual(self.seq.stage, "report")
        self.assertEqual(self.seq.hz, 1450)
        for slot in (108, 110, 112, 114):
            self.send(slot)
        self.assertEqual(self.auto.contact_count, 4)
        self.send(116)
        self.assertEqual(self.seq.stage, "cq")
        self.assertEqual(self.auto.cq_count, 1)
        self.assertTrue(any(e == "contact_abandoned" for e, _ in self.events))

    def test_progress_resets_stage_attempts_and_logs_qso_then_cq(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.send(102)
        self.receive(103, "K1ABC W1AW R-10")
        self.assertEqual(self.seq.stage, "rrr")
        self.assertEqual(self.auto.contact_count, 0)
        self.send(104)
        self.receive(105, "K1ABC W1AW 73")
        self.assertEqual(len(self.saved), 1)
        self.assertTrue(self.auto.active)
        self.assertEqual(self.seq.stage, "cq")
        self.assertEqual(self.auto.cq_count, 0)
        self.send(106)
        self.assertEqual(self.auto.cq_count, 1)

    def test_unrelated_and_repeated_stage_do_not_reset_count(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.send(102)
        self.receive(103, "K1ABC W1AW FN31")
        self.receive(103, "K1ABC W2XYZ -10")
        self.assertEqual(self.auto.contact_count, 1)

    def test_faults_never_restart(self):
        self.send(100)
        self.seq.stop("SWR telemetry lost -- TX stopped")
        self.auto.synchronize()
        self.assertFalse(self.auto.active)
        self.assertIsNone(self.auto.next(102 * 15 + 0.05))

    def test_failed_frame_stops_without_count(self):
        p = self.auto.next(1500.05)
        self.auto.transmitted(p, False, "PTT not confirmed")
        self.assertFalse(self.auto.active)
        self.assertEqual(self.auto.cq_count, 0)
        self.assertFalse(self.auto.finished_no_response)

    def test_logging_failure_stops_sequence(self):
        self.auto.emit = lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
        with self.assertRaises(OSError):
            self.auto.next(1500.05)
        self.assertFalse(self.auto.active)
        self.assertFalse(self.seq.enabled)

    def test_session_files_private_rotate_and_are_reviewable(self):
        with tempfile.TemporaryDirectory() as root:
            log = SessionLog(root, rotate_bytes=700)
            self.auto.emit = log.emit
            self.auto.record("test")
            self.send(100)
            self.receive(101, "CQ W1AW FN31")
            self.auto.stop("Operator stop")
            log.close()
            files = sorted(Path(root).glob("*.jsonl"))
            rows = [
                json.loads(line) for f in files for line in f.read_text().splitlines()
            ]
            self.assertGreater(len(files), 1)
            for f in files:
                self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            self.assertIn("rx_decode", [r["event"] for r in rows])
            for row in rows:
                self.assertIn("utc", row)
                self.assertEqual(row["dial_hz"], 7074000)
            self.assertNotIn("qrz_key", json.dumps(rows))


class SoundTests(unittest.TestCase):
    def test_distinct_short_bounded_pcm(self):
        import array

        a, b = tone("qso"), tone("auto_stop")
        self.assertNotEqual(a, b)
        self.assertLess(len(a), 48000 * 2)
        self.assertLessEqual(max(abs(v) for v in array.array("h", a)), 4200)

    def test_qso_can_be_disabled_but_auto_stop_still_queued(self):
        import queue

        sounds = Sounds.__new__(Sounds)
        sounds.events = queue.Queue()
        with patch("notifications.load_settings", return_value={"qso_beep": False}):
            sounds.play("qso")
            self.assertTrue(sounds.events.empty())
            sounds.play("auto_stop")
            self.assertEqual(sounds.events.get_nowait(), "auto_stop")


class NotificationPlayback(unittest.TestCase):
    def test_speaker_only_playback_waits_for_rx(self):
        import threading
        import time

        ready = threading.Event()
        played = threading.Event()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            played.set()

        with patch("notifications.subprocess.run", side_effect=run):
            sounds = Sounds(ready.is_set)
            try:
                sounds.play("auto_stop")
                self.assertFalse(played.wait(0.12))
                ready.set()
                self.assertTrue(played.wait(1))
                self.assertEqual(calls[0][0][:4], ["aplay", "-q", "-D", "pulse"])
                self.assertIn("alsa_output", calls[0][1]["env"]["PULSE_SINK"])
                self.assertEqual(calls[0][1]["input"], tone("auto_stop"))
            finally:
                sounds.close()


class StationIntegration(unittest.TestCase):
    def test_controller_journals_four_cqs_then_plays_stop_sound(self):
        import threading
        import time
        from unittest.mock import Mock
        from ft8station import Station
        from ft8tx import Job
        from models import DEFAULT_PROFILE

        real_mono = time.monotonic
        clock = [1500.05, 10.05]
        receiver = Mock()
        receiver.frequency = 7074000
        receiver.stop = threading.Event()
        receiver.busy.return_value = True
        receiver.radio_state = {
            "profile": DEFAULT_PROFILE,
            "tx_ready": True,
            "ptt": False,
        }
        decoder = Mock()
        decoder.snapshot.return_value = ([], "", 0)
        decoder.last_decoded_slot = 99
        book = Mock(status="Ready")
        jobs = []

        def queue(pcm, start, dial, **kw):
            job = Job(pcm, start, dial, **kw)
            jobs.append(job)
            return job

        receiver.queue_tx.side_effect = queue

        def wait(test):
            until = real_mono() + 2
            while real_mono() < until:
                if test():
                    return
                time.sleep(0.01)
            self.fail("Controller timed out")

        def advance(wall):
            clock[:] = [wall, wall - 1490]

        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ), patch("time.time", side_effect=lambda: clock[0]), patch(
            "time.monotonic", side_effect=lambda: clock[1]
        ), patch(
            "ft8station.clock_health", return_value=(True, "Synced")
        ), patch(
            "ft8station.load_settings",
            return_value={**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB"},
        ), patch(
            "ft8station.waveform", return_value=bytes(96000)
        ), patch(
            "ft8station.Sounds"
        ) as sounds:
            station = Station(receiver, decoder, book)
            try:
                station.action("auto_cq")
                for i, slot in enumerate((100, 102, 104, 106)):
                    decoder.last_decoded_slot = slot - 1
                    advance(slot * 15 + 0.05)
                    wait(lambda: len(jobs) > i)
                    jobs[i].started.set()
                    advance(slot * 15 + 13.3)
                    jobs[i].success = True
                    jobs[i].done.set()
                    wait(lambda: station.auto.cq_count == i + 1)
                self.assertTrue(station.auto_active)
                decoder.last_decoded_slot = 107
                advance(108 * 15 + 0.05)
                wait(lambda: station.journal is None)
                self.assertFalse(station.auto_active)
                self.assertEqual(len(jobs), 4)
                sounds.return_value.play.assert_called_once_with("auto_stop")
                rows = [
                    json.loads(line)
                    for f in (Path(root) / "icomzero/auto-cq").glob("*.jsonl")
                    for line in f.read_text().splitlines()
                ]
                self.assertEqual(sum(r["event"] == "tx_scheduled" for r in rows), 4)
                self.assertEqual(rows[-1]["event"], "session_stop")
                self.assertTrue(rows[-1]["no_responses"])
            finally:
                station.close()
