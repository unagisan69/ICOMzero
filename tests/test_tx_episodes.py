"""Keep meter-age protection scoped to a single actual RF episode."""

import struct
import unittest
from unittest.mock import patch
from meters import Protection
from models import DEFAULT_PROFILE
from ft8tx import Job, Pacer
import test_rf_protection as fixtures


class TxEpisodes(unittest.TestCase):
    def response(self, civ, state):
        frame = bytes.fromhex("fefee0b61c00") + bytes((state, 0xFD))
        civ.receive(
            bytes(16) + b"\xc1" + struct.pack("<H", len(frame)) + bytes(2) + frame
        )

    def test_confirmed_rx_disarms_age_timer_and_keeps_fault_latched(self):
        guard = Protection(DEFAULT_PROFILE)
        guard.begin(now=10)
        guard.update("swr", 48, 10.1)
        guard.update("alc", 0, 10.1)
        guard.end()
        self.assertIsNone(guard.check(40))
        guard.trip("SWR fault")
        guard.end()
        self.assertEqual(guard.check(60), "SWR fault")

    def test_two_cq_periods_with_rx_gap_and_delayed_ptt_dispatch(self):
        civ, ch, _, _ = fixtures.TransportSafety.make(self)
        civ.tuner_state = 1
        civ.protection.begin(now=10)
        civ.protection.update("swr", 48, 10.1)
        civ.protection.update("alc", 0, 10.1)
        civ.ptt_pending = True
        civ.ptt_expected = False
        self.response(civ, 0)
        # Exact reported failure: next slot is 30 seconds later, and the
        # command pacer has not yet sent its newly requested PTT-on.
        jobs = [Job(bytes(96000), 40.5, 7074000)]
        job = jobs[0]
        packets = []
        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            civ.ptt,
            packets.append,
            lambda _: True,
            lambda: civ.ptt_state,
            lambda _: None,
            protection=civ.protection.check,
        )
        civ.next_command = 40.04
        with patch("time.monotonic", return_value=40.0):
            p.tick(40, 40)
            civ.tick(40)
        with patch("time.monotonic", return_value=40.01):
            p.tick(40.01, 40.01)
            civ.tick(40.01)
        self.assertIsNone(civ.protection.fault)
        self.assertFalse(job.done.is_set())
        with patch("time.monotonic", return_value=40.05):
            civ.tick(40.05)
        self.assertEqual(civ.protection.started, 40.05)
        self.response(civ, 1)
        civ.protection.update("swr", 48, 40.1)
        civ.protection.update("alc", 0, 40.1)
        with patch("time.monotonic", return_value=40.5):
            p.tick(40.5, 40.5)
        self.assertTrue(packets)
        self.assertTrue(job.started.is_set())
        self.assertIsNone(civ.protection.fault)

    def test_early_rx_reply_does_not_end_new_tx_guard(self):
        civ, _, _, _ = fixtures.TransportSafety.make(self)
        civ.ptt_pending = True
        civ.ptt_expected = True
        civ.protection.begin(now=10)
        self.response(civ, 0)
        self.assertEqual(civ.protection.started, 10)
        self.assertTrue(civ.ptt_pending)
        self.assertIn("telemetry lost", civ.protection.check(12.01))

    def test_kenwood_ends_only_on_matching_rx_confirmation(self):
        from kenwood import Cat

        cat = Cat(
            lambda _: None,
            "user",
            "password",
            False,
            lambda _: None,
            lambda *_: None,
            lambda *_: None,
            True,
        )
        cat.ptt(True)
        cat.receive("IF" + "0" * 26 + "0" + "0" * 8)
        self.assertTrue(cat.ptt_pending)
        self.assertIsNotNone(cat.protection.started)
        cat.receive("IF" + "0" * 26 + "1" + "0" * 8)
        cat.ptt(False)
        cat.receive("IF" + "0" * 26 + "0" + "0" * 8)
        self.assertFalse(cat.ptt_pending)
        self.assertIsNone(cat.protection.started)

    def test_fast_tuner_completion_ends_age_guard_in_either_reply_order(self):
        for order in (("ptt", "tuner"), ("tuner", "ptt")):
            with self.subTest(order=order):
                civ, _, _, state = fixtures.TransportSafety.make(self)
                civ.tuning_requested = True
                civ.tune_started = 10
                civ.tune_seen_active = True
                civ.tuner_state = 2
                civ.ptt_state = True
                civ.protection.begin(tuning=True, now=10)
                for i, kind in enumerate(order):
                    with patch("time.monotonic", return_value=10.4 + i * 0.05):
                        if kind == "ptt":
                            self.response(civ, 0)
                        else:
                            frame = bytes.fromhex("fefee0b61c0101fd")
                            civ.receive(
                                bytes(16)
                                + b"\xc1"
                                + struct.pack("<H", len(frame))
                                + bytes(2)
                                + frame
                            )
                    if i == 0:
                        self.assertTrue(civ.tuning_requested)
                self.assertFalse(civ.tuning_requested)
                self.assertIsNone(civ.protection.started)
                self.assertIsNone(civ.protection.check(14))
                self.assertGreater(state["tune_completed_at"], 10)

    def test_cached_pre_tune_rx_cannot_disarm_live_tuner_protection(self):
        civ, _, _, _ = fixtures.TransportSafety.make(self)
        civ.tuning_requested = civ.tune_seen_active = True
        civ.tune_started = 10
        civ.ptt_at = 9
        civ.ptt_state = False
        civ.tuner_at = 10.4
        civ.tuner_state = 1
        civ.protection.begin(tuning=True, now=10)
        with patch("time.monotonic", return_value=10.45):
            civ.finish_tuner_if_idle()
        self.assertTrue(civ.tuning_requested)
        self.assertIn("telemetry lost", civ.protection.check(13.01))
