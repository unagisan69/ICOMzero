from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch
from desktop_ui import Spectrum, DesktopApp
from desktop_notifications import DesktopAlerts
from ft8 import History, Message


class RetainedExchange(unittest.TestCase):
    def test_logged_exchange_stays_but_future_cq_is_hidden(self):
        h = History()
        rows = [
            Message(1, 1500, "CQ W1AW FN31", -10),
            Message(2, 1500, "K4TTL W1AW R-10", -10),
            Message(3, 1500, "W1AW K4TTL RRR", 0, True),
        ]
        h.update(rows, "K4TTL", partner="W1AW")
        h.retain_contact("W1AW")
        future = Message(4, 1500, "CQ W1AW FN31", -10)
        h.update(rows + [future], "K4TTL", frozenset({"W1AW"}))
        self.assertEqual(h.messages, rows)
        late = Message(5, 1500, "K4TTL W1AW RR73", -10)
        h.update(rows + [future, late], "K4TTL", frozenset({"W1AW"}))
        self.assertEqual(h.messages, rows + [late])
        h.update([future], "K4TTL", frozenset({"W1AW"}))
        self.assertFalse(h.messages)
        self.assertFalse(h.retained)


class DesktopSpectrum(unittest.TestCase):
    def test_retune_reprojects_history_in_absolute_frequency(self):
        plot = Spectrum()
        bins = bytes([80] * 100)
        plot.update((1, 7000000, 7100000, bins), 7050000, 100, 40, 1)
        color = plot.image.getpixel((50, 0))
        plot.update((2, 7010000, 7110000, bytes([1] * 100)), 7060000, 100, 40, 1)
        self.assertEqual(plot.image.getpixel((40, 1)), color)
        self.assertEqual(plot.image.getpixel((99, 1)), (8, 15, 32))

    def test_click_uses_visible_absolute_edges_and_ignores_labels_and_tx(self):
        ui = DesktopApp.__new__(DesktopApp)
        rx = SimpleNamespace(busy=lambda: True, tune=Mock(), radio_state={})
        ui.sessions = SimpleNamespace(rx=rx, mode="radio", switching=False)
        ui.radio_canvas = Mock()
        ui.radio_canvas.winfo_width.return_value = 1032
        ui.radio_canvas.winfo_height.return_value = 400
        ui.spectrum = SimpleNamespace(edges=(7000000, 7100000))
        ui.safe = lambda fn: fn()
        ui.click_spectrum(SimpleNamespace(x=516, y=180))
        rx.tune.assert_called_once_with(7050050)
        rx.tune.reset_mock()
        ui.click_spectrum(SimpleNamespace(x=516, y=399))
        rx.radio_state["ptt"] = True
        ui.click_spectrum(SimpleNamespace(x=516, y=180))
        rx.tune.assert_not_called()


class DesktopNotificationTests(unittest.TestCase):
    def test_notifications_run_off_thread_and_deduplicate_errors(self):
        called = threading.Event()

        def notify(*args, **kwargs):
            called.set()

        with patch("desktop_notifications.subprocess.run", side_effect=notify) as run:
            alerts = DesktopAlerts()
            try:
                alerts.send("ICOMzero error", "Radio disconnected", error=True)
                alerts.send("ICOMzero error", "Radio disconnected", error=True)
                self.assertTrue(called.wait(1))
            finally:
                alerts.close()
            self.assertEqual(run.call_count, 1)
            self.assertIn("--urgency=critical", run.call_args.args[0])

    def test_missing_desktop_notification_service_cannot_break_app(self):
        with patch(
            "desktop_notifications.subprocess.run", side_effect=FileNotFoundError
        ):
            alerts = DesktopAlerts()
            alerts.send("QSO logged", "W1AW")
            alerts.close()
