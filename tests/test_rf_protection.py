import unittest
from unittest.mock import patch
from collections import OrderedDict
from meters import Protection, interpolate
from models import DEFAULT_PROFILE
from ft8 import History, Message, Decoder
from ft8qso import Sequencer
from ft8log import DEFAULTS
from ft8tx import Job, Pacer
from civ import Civ


class MeterProtection(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.guard = Protection(DEFAULT_PROFILE, self.state.__setitem__)
        self.guard.begin(now=10)

    def sample(self, swr=48, alc=0, now=10.1):
        self.guard.update("swr", swr, now)
        self.guard.update("alc", alc, now)

    def test_calibrated_swr_threshold_latches_until_explicit_clear(self):
        self.sample(swr=80)
        self.assertIsNone(self.guard.check(10.1))  # Exactly 2.0 allowed.
        self.sample(swr=81, now=10.2)
        self.assertIn("SWR", self.guard.check(10.2))
        self.sample(swr=0, now=10.3)
        self.guard.begin(now=11)
        self.assertIsNotNone(self.guard.check(11))
        self.guard.clear()
        self.assertIsNone(self.guard.check(12))
        self.assertIsNone(self.state["tx_fault"])

    def test_alc_redline_and_sustained_lower_alc_stop(self):
        self.sample(alc=120)
        self.assertIn("ALC too high", self.guard.check(10.1))
        self.guard.clear()
        self.guard.begin(now=20)
        self.sample(alc=30, now=20.1)
        self.assertIsNone(self.guard.check(20.1))
        self.sample(alc=30, now=20.41)
        self.assertIn("ALC sustained", self.guard.check(20.41))

    def test_tuner_uses_swr_but_not_external_audio_alc(self):
        self.guard.begin(tuning=True, now=10)
        self.sample(alc=255)
        self.assertIsNone(self.guard.check(10.1))
        self.sample(swr=81, now=10.2)
        self.assertIn("SWR", self.guard.check(10.2))

    def test_missing_and_stale_telemetry_fail_closed(self):
        self.assertIsNone(self.guard.check(11.19))
        self.assertIn("telemetry lost", self.guard.check(11.21))
        self.guard.clear()
        self.guard.begin(now=20)
        self.sample(now=20.1)
        self.assertIsNone(self.guard.check(21.2))
        self.assertIn("telemetry lost", self.guard.check(21.31))

    def test_power_interpolation_and_invalid_values(self):
        self.assertEqual(interpolate(DEFAULT_PROFILE["meters"]["watts"], 213), 100)
        self.guard.update("swr", None, 10.1)
        self.assertIn("Invalid SWR", self.guard.check(10.1))

    def test_fault_stops_audio_and_unkeys_pacer(self):
        jobs = [Job(bytes(96000), 100.5, 7074000)]
        keyed = [False]
        packets = []

        def ptt(value):
            keyed[0] = value

        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            ptt,
            packets.append,
            lambda _: True,
            lambda: keyed[0],
            lambda _: None,
            protection=lambda: self.guard.check(10.2),
        )
        p.tick(100.4, 50.4)
        self.sample(swr=81, now=10.2)
        job = p.job
        p.tick(100.5, 50.5)
        self.assertFalse(keyed[0])
        self.assertFalse(packets)
        self.assertTrue(job.done.is_set())
        self.assertIn("SWR", job.error)


class SelectionAndLocalTx(unittest.TestCase):
    def test_bottom_releases_selection_and_follows_new_rows(self):
        h = History()
        rows = [Message(i, 1500, "CQ W1AW FN31", -10) for i in range(20)]
        h.update(rows, "K1ABC")
        h.move(-1)
        h.move(-1)
        h.move(1)
        self.assertIsNone(h.selected)
        new = Message(20, 1500, "CQ W2AW FN31", -10)
        h.update(rows + [new], "K1ABC")
        self.assertEqual(h.visible()[-1], new)

    def test_local_transmission_visible_but_never_drives_contact(self):
        decoder = Decoder()
        try:
            decoder.record_tx(100, 1500, "W1AW K1ABC FN42")
            h = History()
            h.update(decoder.snapshot()[0], "K1ABC")
            self.assertTrue(h.messages[0].local_tx)
            seq = Sequencer(
                {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42"}, lambda _: None
            )
            with patch("time.time", return_value=1500.1):
                seq.cq(7074000)
            stage = seq.stage
            seq.receive(Message(101, 1500, "K1ABC W1AW FN31", -10, local_tx=True))
            self.assertEqual(seq.stage, stage)
        finally:
            decoder.close()


class TransportSafety(unittest.TestCase):
    def make(self):
        class Channel:
            ready = True

            def __init__(self):
                self.sent = []
                self.history = OrderedDict()
                self.seq = 0

            def send(self, *_, **kw):
                self.sent.append(kw["body"][9:-1])
                self.history[self.seq] = kw["body"]
                self.seq += 1

        ch = Channel()
        commands = []
        state = {}

        def take():
            result = commands[:]
            commands.clear()
            return result

        civ = Civ(
            ch,
            0xB6,
            lambda _: None,
            lambda *_: None,
            lambda: None,
            take,
            state.__setitem__,
            DEFAULT_PROFILE,
            True,
        )
        civ.open = True
        civ.mode = 1
        civ.frequency = 7074000
        return civ, ch, commands, state

    def test_power_requires_readback_and_restores_original(self):
        civ, ch, commands, state = self.make()
        civ.original_power = civ.current_power = 255
        commands.append(("ft8_power", 10))
        civ.tick(1)
        for i in range(25):
            civ.tick(1.03 + i * 0.03)
        self.assertEqual(civ.power_target, 26)
        self.assertFalse(state["power_ready"])
        self.assertIn(bytes.fromhex("140a0026"), ch.sent)
        civ.current_power = 26
        civ.tick(2)
        self.assertTrue(state["power_ready"])
        commands.append(("ft8_power", 20))
        civ.tick(2.01)  # Cancel before the second setting is applied.
        civ.ptt_state = False
        civ.ptt_pending = False
        civ.close()
        self.assertIn(bytes.fromhex("140a0255"), ch.sent)

    def test_latched_fault_prevents_even_direct_ptt_request(self):
        civ, ch, _, _ = self.make()
        civ.protection.trip("SWR fault")
        civ.ptt(True)
        civ.tick(1)
        self.assertNotIn(bytes.fromhex("1c0001"), ch.sent)

    def test_tuner_start_not_retained_for_late_replay(self):
        civ, ch, _, _ = self.make()
        civ.send(bytes.fromhex("1c0102"))
        self.assertFalse(ch.history)
        self.assertTrue(civ.protection.tuning)
        civ.protection.update("swr", 81)
        civ.tuning_requested = True
        civ.tick(1)
        self.assertIsNotNone(civ.protection.fault)
        for i in range(8):
            civ.tick(1.03 + i * 0.03)
        self.assertIn(bytes.fromhex("1c0100"), ch.sent)


class AutomaticTunerController(unittest.TestCase):
    def test_waits_for_power_and_readback_and_blocks_contacts_while_tuning(self):
        import threading
        import time
        from unittest.mock import Mock
        from ft8station import Station

        receiver = Mock()
        receiver.frequency = 7074000
        receiver.stop = threading.Event()
        receiver.busy.return_value = True
        receiver.radio_state = {
            "profile": DEFAULT_PROFILE,
            "power_ready": False,
            "ptt": False,
            "ptt_pending": False,
        }
        decoder = Mock()
        decoder.snapshot.return_value = ([], "", 0)

        def wait(test):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if test():
                    return
                time.sleep(0.01)
            self.fail("Controller did not reach expected state")

        with patch("ft8station.clock_health", return_value=(True, "Synced")), patch(
            "ft8station.load_settings",
            return_value={**DEFAULTS, "callsign": "K1ABC", "grid": "FN42"},
        ):
            station = Station(receiver, decoder, Mock())
            try:
                station.action("tune", dial=7074000)
                wait(lambda: station.tuning)
                self.assertNotIn(
                    ("tuner",), [c.args for c in receiver.command.call_args_list]
                )
                station.action("cq")
                self.assertFalse(station.sequence.enabled)
                receiver.radio_state["power_ready"] = True
                wait(
                    lambda: ("tuner",)
                    in [c.args for c in receiver.command.call_args_list]
                )
                receiver.radio_state.update(
                    tuner=1,
                    tuner_at=time.monotonic() + 0.01,
                    tune_started=time.monotonic(),
                )
                wait(lambda: not station.tuning)
                self.assertEqual(station.status, "Antenna tuner ready")
                receiver.queue_tx.assert_not_called()
            finally:
                station.close()


class RecoveryRegressions(unittest.TestCase):
    make = TransportSafety.make

    def test_stop_preserves_successful_inline_tuner(self):
        civ, ch, commands, state = self.make()
        civ.tuner_state = 1
        commands.append(("tuner_stop", None))
        for i in range(20):
            civ.tick(10 + i * 0.03)
        self.assertNotIn(bytes.fromhex("1c0100"), ch.sent)
        self.assertEqual(civ.tuner_state, 1)

    def test_clear_retries_until_fresh_rx_and_tuner_confirmation(self):
        civ, ch, commands, state = self.make()
        civ.protection.trip("SWR telemetry lost")
        civ.tuner_state = 2
        civ.ptt_state = True
        commands.append(("clear_fault", None))
        with patch("civ.time.monotonic", return_value=10):
            civ.tick(10)
        self.assertIsNotNone(civ.clear_requested)
        self.assertIsNotNone(civ.protection.fault)
        civ.ptt_state = False
        civ.ptt_pending = False
        civ.tuner_state = 0
        civ.ptt_at = civ.tuner_at = 10.1
        civ.tick(10.1)
        self.assertIsNone(civ.protection.fault)
        self.assertEqual(state["fault_clear_status"], "Warning cleared")

    def test_warning_clear_does_not_bypass_an_already_tuned_antenna(self):
        civ, ch, commands, state = self.make()
        civ.protection.trip("SWR telemetry lost")
        civ.tuner_state = 1
        commands.append(("clear_fault", None))
        with patch("civ.time.monotonic", return_value=10):
            civ.tick(10)
        civ.ptt_at = civ.tuner_at = 10.1
        civ.ptt_pending = False
        for i in range(20):
            civ.tick(10.1 + i * 0.03)
        self.assertIsNone(civ.protection.fault)
        self.assertNotIn(bytes.fromhex("1c0100"), ch.sent)

    def test_pending_off_requires_confirmation_then_stops_retrying(self):
        jobs = [Job(bytes(960), 100.5, 7074000)]
        commands = []
        pending = [False]
        keyed = [False]

        def ptt(value):
            commands.append(value)
            pending[0] = True

        p = Pacer(
            lambda: jobs.pop() if jobs else None,
            ptt,
            lambda _: None,
            lambda _: True,
            lambda: keyed[0],
            lambda _: None,
            off_confirmed=lambda: not keyed[0] and not pending[0],
        )
        job = jobs[0]
        job.cancel.set()
        p.tick(100, 50)
        self.assertFalse(job.done.is_set())
        pending[0] = False
        p.tick(100.05, 50.05)
        self.assertTrue(job.done.is_set())
        before = len(commands)
        keyed[0] = True  # Independent tuner starts.
        p.tick(100.2, 50.2)
        self.assertEqual(len(commands), before)
