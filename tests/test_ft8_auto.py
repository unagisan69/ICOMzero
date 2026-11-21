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
        self.auto.prepare()
        pending = self.auto.next()
        self.assertIsNotNone(pending)
        self.auto.transmitted(pending, True)
        return pending

    def receive(self, slot, text):
        self.now.return_value = (slot + 1) * 15 + 0.02
        self.auto.receive(Message(slot, 1450, text, -12))

    def test_two_cqs_wait_full_receive_then_stop(self):
        for slot in (100, 102):
            self.send(slot)
        self.assertTrue(self.auto.active)
        self.assertIsNone(self.auto.next(103 * 15 + 0.05))
        self.assertIsNone(self.auto.next(104 * 15 + 0.05))
        self.assertFalse(self.auto.active)
        self.assertTrue(self.auto.finished_no_response)
        self.assertEqual(self.seq.repeat_limit, 5)

    def test_last_receive_reply_gets_contact_attempts_then_returns_cq(self):
        for slot in (100, 102):
            self.send(slot)
        self.receive(103, "K1ABC W1AW FN31")
        self.assertEqual(self.seq.stage, "report")
        self.assertEqual(self.seq.hz, 1450)
        for slot in (104, 106, 108, 110):
            self.send(slot)
        self.assertEqual(self.auto.contact_count, 4)
        self.send(112)
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
    def test_controller_journals_two_cqs_then_plays_stop_sound(self):
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
                for i, slot in enumerate((100, 102)):
                    decoder.last_decoded_slot = slot - 1
                    advance(slot * 15 + 0.05)
                    wait(lambda: len(jobs) > i)
                    jobs[i].started.set()
                    advance(slot * 15 + 13.3)
                    jobs[i].success = True
                    jobs[i].done.set()
                    wait(lambda: station.auto.cq_count == i + 1)
                self.assertTrue(station.auto_active)
                decoder.last_decoded_slot = 103
                advance(104 * 15 + 0.05)
                wait(lambda: station.journal is None)
                self.assertFalse(station.auto_active)
                self.assertEqual(len(jobs), 2)
                sounds.return_value.play.assert_called_once_with("auto_stop")
                rows = [
                    json.loads(line)
                    for f in (Path(root) / "icomzero/auto-cq").glob("*.jsonl")
                    for line in f.read_text().splitlines()
                ]
                self.assertEqual(sum(r["event"] == "tx_scheduled" for r in rows), 2)
                self.assertEqual(rows[-1]["event"], "session_stop")
                self.assertTrue(rows[-1]["no_responses"])
            finally:
                station.close()


class QueueAndHunt(unittest.TestCase):
    setUp = AutoTests.setUp
    send = AutoTests.send
    receive = AutoTests.receive

    def cq(self, slot, call, snr, hz=1700):
        self.now.return_value = (slot + 1) * 15 - 0.4
        self.auto.receive(Message(slot, hz, f"CQ {call} FN31", snr))

    def test_two_strongest_fresh_distinct_targets_get_two_calls_each(self):
        self.send(100)
        self.cq(101, "W1AW", -15)
        self.cq(101, "W2XYZ", 5, 1900)
        self.send(102)
        self.cq(103, "W3ABC", -2, 2100)
        self.send(104)
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertEqual(self.seq.hz, 1900)
        self.send(106)
        self.send(108)
        self.assertEqual(self.seq.partner, "W3ABC")
        self.send(110)
        self.now.return_value = 112 * 15 + 0.05
        self.assertIsNone(self.auto.next())
        self.assertFalse(self.auto.active)
        selected = [d["call"] for e, d in self.events if e == "target_selected"]
        self.assertEqual(selected, ["W2XYZ", "W3ABC"])

    def test_latest_snr_used_and_busy_or_stale_cqs_excluded(self):
        self.send(100)
        self.cq(101, "W1AW", 20)
        self.cq(101, "W2XYZ", 10)
        self.send(102)
        self.cq(103, "W1AW", -15)
        self.now.return_value = 104 * 15 + 0.05
        self.auto.receive(Message(103, 1900, "W9XYZ W2XYZ -10", 10))
        self.send(104)
        self.assertEqual(self.seq.partner, "W1AW")
        self.assertEqual(self.auto.fallback_used, ["W1AW"])
        self.assertNotIn("W2XYZ", self.auto.cqs)

    def test_queued_caller_gets_priority_after_logged_contact(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.receive(101, "K1ABC W2XYZ FN32")
        self.receive(101, "K1ABC W2XYZ FN32")
        self.assertEqual(list(self.auto.queue), ["W2XYZ"])
        self.send(102)
        self.receive(103, "K1ABC W1AW R-10")
        self.receive(103, "K1ABC W2XYZ FN32")
        self.send(104)
        self.receive(105, "K1ABC W1AW 73")
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertEqual(self.seq.stage, "report")
        self.assertFalse(self.auto.queue)
        self.assertEqual(len(self.saved), 1)
        self.assertTrue(
            any(
                e == "target_selected" and d["source"] == "queued"
                for e, d in self.events
            )
        )

    def test_queue_ttl_and_oldest_first_refresh_dedup(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.receive(101, "K1ABC W2XYZ FN32")
        self.receive(103, "K1ABC W3ABC FN33")
        self.receive(105, "K1ABC W2XYZ FN32")
        self.assertEqual(list(self.auto.queue), ["W2XYZ", "W3ABC"])
        self.now.return_value = 114 * 15
        self.auto._prune(self.now.return_value)
        self.assertFalse(self.auto.queue)
        self.assertTrue(any(e == "queue_expired" for e, _ in self.events))

    def test_fallback_reply_promotes_to_normal_exchange_four_attempt_limit(self):
        self.send(100)
        self.cq(101, "W1AW", 5)
        self.send(102)
        self.cq(103, "W1AW", 5)
        self.send(104)
        self.assertTrue(self.auto.hunt_waiting)
        self.receive(105, "K1ABC W1AW -10")
        self.assertFalse(self.auto.hunt_waiting)
        self.assertEqual(self.seq.stage, "rreport")
        for slot in (106, 108, 110, 112):
            self.send(slot)
        self.assertEqual(self.auto.contact_count, 4)
        self.send(114)
        self.assertEqual(self.seq.stage, "cq")

    def test_prepares_target_after_rx_decode_before_keyup_and_keeps_opposite_period(
        self,
    ):
        self.send(100)
        self.cq(101, "W1AW", 5)
        self.send(102)
        self.cq(103, "W1AW", 5)
        self.now.return_value = 104 * 15 - 0.4
        self.assertTrue(self.auto.prepare(decoded_slot=103))
        self.assertEqual(self.seq.partner, "W1AW")
        self.assertEqual(self.seq.parity, 0)
        self.assertIsNone(self.auto.next())
        self.send(104)

    def test_completed_caller_is_not_automatically_worked_twice(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.send(102)
        self.receive(103, "K1ABC W1AW R-10")
        self.send(104)
        self.receive(105, "K1ABC W1AW 73")
        self.send(106)
        self.receive(107, "K1ABC W1AW FN31")
        self.assertEqual(self.seq.stage, "cq")
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.events[-1][1]["decision"], "already_worked_this_session")

    def test_cq_freshness_and_snr_tie_recency(self):
        self.send(100)
        self.cq(101, "W1AW", 8)
        self.send(102)
        self.cq(103, "W2XYZ", 8)
        self.send(104)
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.auto.cqs["W3ABC"] = Message(100, 1600, "CQ W3ABC FN33", 30)
        self.now.return_value = 110 * 15 + 0.05
        self.auto._prune(self.now.return_value)
        self.assertNotIn("W3ABC", self.auto.cqs)

    def test_queued_caller_gets_priority_after_contact_exhaustion(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        for slot in (102, 104, 106, 108):
            self.send(slot)
        self.receive(109, "K1ABC W2XYZ FN32")
        self.send(110)
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertEqual(self.seq.stage, "report")

    def test_duplicate_cq_in_same_slot_keeps_stronger_decode(self):
        self.send(100)
        self.cq(101, "W1AW", 10, 1700)
        self.cq(101, "W1AW", -5, 1701)
        self.assertEqual(self.auto.cqs["W1AW"].snr, 10)
        self.assertEqual(self.auto.cqs["W1AW"].hz, 1700)

    def test_unsupported_compound_cq_skipped_in_favor_of_encodable_call(self):
        self.send(100)
        self.cq(101, "W1AW/5", 20)
        self.cq(101, "W2XYZ", 5)
        self.send(102)
        self.cq(103, "W1AW/5", 20)
        self.cq(103, "W2XYZ", 5)
        self.send(104)
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertNotIn("W1AW/5", self.auto.cqs)

    def test_late_caller_in_completion_batch_preempts_new_cq(self):
        self.send(100)
        self.receive(101, "K1ABC W1AW FN31")
        self.send(102)
        self.receive(103, "K1ABC W1AW R-10")
        self.send(104)
        self.receive(105, "K1ABC W1AW 73")
        self.receive(105, "K1ABC W2XYZ FN32")
        self.send(106)
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertEqual(self.seq.stage, "report")
