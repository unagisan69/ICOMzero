import threading
import unittest
from unittest.mock import Mock, patch
from ft8station import Station
from ft8log import DEFAULTS
from models import DEFAULT_PROFILE


class ReadinessRecovery(unittest.TestCase):
    def run_steps(self, steps):
        clock = [0.0]
        receiver = Mock()
        receiver.frequency = 7074000
        receiver.stop = threading.Event()
        receiver.busy.return_value = True
        receiver.radio_state = {
            "profile": DEFAULT_PROFILE,
            "tx_ready": False,
            "ptt": False,
        }
        decoder = Mock()
        decoder.rate = 12000
        decoder.snapshot.return_value = ([], "", 0)
        decoder.last_decoded_slot = None
        settings = {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42"}
        remaining = iter(steps)

        class Steps:
            def wait(self, _):
                try:
                    stamp, ready = next(remaining)
                except StopIteration:
                    return True
                clock[0] = stamp
                receiver.radio_state["tx_ready"] = ready
                return False

            def is_set(self):
                return False

        with patch("ft8station.threading.Thread.start"), patch(
            "ft8station.Sounds"
        ), patch("ft8station.load_settings", return_value=settings), patch(
            "ft8station.clock_health", return_value=(True, "Synced")
        ), patch(
            "ft8station.waveform", return_value=b"pcm"
        ), patch(
            "ft8station.record_fault"
        ) as diagnostic, patch(
            "time.monotonic", side_effect=lambda: clock[0]
        ), patch(
            "time.time", side_effect=lambda: 1502 + clock[0]
        ):
            station = Station(receiver, decoder, Mock())
            station.closed = Steps()
            station.sequence.cq(7074000)
            station.sequence.next = Mock(return_value=None)
            station.run()
            return station.sequence, diagnostic.call_args_list

    def test_brief_recovery_after_long_success_does_not_reuse_startup_deadline(self):
        sequence, diagnostics = self.run_steps(
            [(0, True), (20, True), (21, False), (23, True)]
        )
        self.assertTrue(sequence.enabled)
        self.assertFalse(diagnostics)

    def test_initial_setup_still_has_an_eight_second_limit(self):
        sequence, diagnostics = self.run_steps([(0, False), (7.9, False), (8.1, False)])
        self.assertFalse(sequence.enabled)
        self.assertEqual(sequence.status, "LAN modulation setup not confirmed")
        self.assertEqual(len(diagnostics), 1)

    def test_continuous_runtime_readiness_loss_is_bounded_and_logged(self):
        sequence, diagnostics = self.run_steps(
            [(0, True), (20, True), (21, False), (28.1, False)]
        )
        self.assertFalse(sequence.enabled)
        self.assertEqual(sequence.status, "Radio TX settings not confirmed")
        self.assertEqual(len(diagnostics), 1)
