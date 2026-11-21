import unittest
from unittest.mock import patch
from ft8 import Message
from ft8auto import AutoCQ
from ft8qso import Sequencer
from ft8log import DEFAULTS


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("time.time", return_value=1500.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.saved = []
        self.events = []
        self.seq = Sequencer(
            {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB"}, self.saved.append
        )
        self.auto = AutoCQ(self.seq, lambda e, **kw: self.events.append((e, kw)))
        self.auto.start(7074000, mode="pota")

    def rx(self, slot, text, snr=-12):
        self.now.return_value = (slot + 1) * 15 - 0.4
        self.auto.receive(Message(slot, 1650, text, snr))

    def tx(self, slot):
        self.now.return_value = slot * 15 + 0.05
        self.auto.prepare()
        p = self.auto.next()
        self.assertIsNotNone(p)
        self.auto.transmitted(p, True)
        return p

    def test_monitor_never_calls_cq_or_answers_ordinary_cq(self):
        self.rx(101, "CQ W1AW FN31")
        self.auto.prepare()
        self.assertEqual(self.seq.stage, "monitor")
        self.assertIsNone(self.auto.next(1530.05))
        self.assertTrue(self.auto.active)

    def test_activation_starts_direct_rreport_at_opposite_period(self):
        for tag in ("POTA", "SOTA"):
            with self.subTest(tag=tag):
                self.auto.start(7074000, mode="pota")
                self.rx(101, f"CQ {tag} W1AW FN31")
                self.auto.prepare()
                self.assertEqual(self.seq.message, "W1AW K1ABC R-12")
                self.assertEqual(self.seq.hz, 1650)
                self.assertEqual(self.seq.parity, 0)
                self.assertIsNone(self.seq.received)
                self.assertFalse(any(e == "tx_scheduled" for e, _ in self.events))

    def test_three_unanswered_sends_rest_then_monitor_without_own_cq(self):
        self.rx(101, "CQ POTA W1AW FN31")
        for slot in (102, 104, 106):
            self.tx(slot)
        self.now.return_value = 108 * 15 + 0.05
        self.auto.prepare()
        self.assertEqual(self.seq.stage, "monitor")
        self.assertTrue(self.auto.active)
        self.assertIsNone(self.auto.next())
        self.rx(108, "CQ SOTA W2XYZ FN32")
        self.assertFalse(self.auto.prepare())
        self.now.return_value = 110 * 15 + 0.05
        self.assertTrue(self.auto.prepare())
        self.assertEqual(self.seq.partner, "W2XYZ")
        self.assertFalse(
            any(
                e == "tx_scheduled" and kw["text"].startswith("CQ ")
                for e, kw in self.events
            )
        )

    def test_early_ack_does_not_invent_received_report_or_log(self):
        self.rx(101, "CQ POTA W1AW FN31")
        self.tx(102)
        self.rx(103, "K1ABC W1AW RRR")
        self.assertIsNone(self.seq.received)
        self.assertEqual(self.seq.stage, "rreport")
        self.assertFalse(self.saved)

    def test_actual_report_and_ack_complete_contact_then_monitor(self):
        self.rx(101, "CQ POTA W1AW FN31")
        self.tx(102)
        self.rx(103, "K1ABC W1AW -10")
        self.tx(104)
        self.rx(105, "K1ABC W1AW RRR")
        self.assertEqual(self.seq.stage, "73")
        self.tx(106)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual((self.saved[0]["sent"], self.saved[0]["received"]), (-12, -10))
        self.assertEqual(self.seq.stage, "monitor")
        self.assertTrue(self.auto.active)

    def test_fault_stops_monitoring_permanently_until_explicit_start(self):
        self.seq.stop("SWR telemetry lost — TX stopped")
        self.auto.synchronize()
        self.assertFalse(self.auto.active)
        self.rx(101, "CQ POTA W1AW FN31")
        self.assertIsNone(self.auto.next(1530.05))


class AutoPolicy(unittest.TestCase):
    def test_one_own_cq_then_five_distinct_targets_twice_each(self):
        now = [1500.0]
        events = []
        seq = Sequencer(
            {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB"}, lambda q: None
        )
        auto = AutoCQ(seq, lambda e, **kw: events.append((e, kw)))
        with patch("time.time", side_effect=lambda: now[0]):
            auto.start(7074000)
            p = auto.next(1500.05)
            auto.transmitted(p, True)
            calls = ["W1AAA", "W2BBB", "W3CCC", "W4DDD", "W5EEE"]
            for slot in range(101, 123):
                now[0] = slot * 15 + 0.05
                if slot % 2:
                    for i, call in enumerate(calls):
                        auto.receive(
                            Message(slot, 1000 + i * 200, f"CQ {call} FN31", 10 - i)
                        )
                else:
                    auto.prepare()
                    p = auto.next()
                    if p:
                        auto.transmitted(p, True)
            now[0] = 124 * 15 + 0.05
            auto.prepare()
            self.assertFalse(auto.active)
        texts = [kw["text"] for e, kw in events if e == "tx_scheduled"]
        self.assertEqual(sum(t.startswith("CQ ") for t in texts), 1)
        self.assertEqual(len(texts), 11)
        for call in calls:
            self.assertEqual(sum(t.startswith(call + " ") for t in texts), 2)
