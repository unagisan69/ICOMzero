import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import numpy as np
from desktop_session import Sessions
from desktop_ui import Spectrum
from models import DEFAULT_PROFILE
from ft8 import Message


class SessionsTests(unittest.TestCase):
    def engine(self):
        rx = Mock()
        rx.session_revision = 1
        rx.frequency = 7074000
        rx.radio_state = {
            "profile": DEFAULT_PROFILE,
            "mode": 1,
            "nr": False,
            "ptt": False,
            "data_mode": DEFAULT_PROFILE.get("ft8_data_mode", 1),
        }
        rx.radio_state.update(
            {key: 0 for key in DEFAULT_PROFILE.get("tx_controls", {})}
        )
        rx.stop = threading.Event()
        rx.thread = None
        rx.busy.return_value = True
        decoder = Mock()
        decoder.snapshot.return_value = (
            [Message(100, 1500, "CQ W1AW FN31", -10)],
            "Ready",
            0.5,
        )
        station = Mock()
        station.current_partner = ""
        station.active = True
        station.auto_active = True
        station.tuning = False
        station.sequence.enabled = True
        app = {
            "Receiver": lambda: rx,
            "RATE": 48000,
            "FT8_RATE": 12000,
            "load": lambda: ["fixture"],
            "connection": lambda _: ("fixture",),
            "read_config": lambda: {"Radio": "Auto (Icom)"},
        }
        book = Mock()
        book.worked_calls.return_value = set()
        manager = Sessions(
            app,
            book,
            receiver=lambda: rx,
            decoder=lambda **_: decoder,
            station=lambda *_: station,
            cw=lambda **_: Mock(),
        )
        return manager, rx, decoder, station, book

    def test_log_and_settings_do_not_restart_or_stop_an_active_session(self):
        for mode in ("radio", "ft8"):
            manager, rx, decoder, station, book = self.engine()
            manager.mode = mode
            manager.rx = rx
            manager.decoder = decoder
            manager.station = station
            with patch.object(manager, "switch") as switch:
                for _ in range(4):
                    manager.select_view("log")
                    manager.select_view("settings")
            switch.assert_not_called()
            rx.stop_tx.assert_not_called()
            station.action.assert_not_called()
            decoder.activate.assert_not_called()
            book.close.assert_not_called()

    def test_only_radio_ft8_view_handoff_changes_session(self):
        manager, *_ = self.engine()
        manager.mode = "ft8"
        with patch.object(manager, "switch") as switch:
            manager.select_view("ft8")
            manager.select_view("log")
            manager.select_view("radio")
        switch.assert_called_once_with("radio")

    def test_view_change_during_handoff_is_applied_after_worker_finishes(self):
        manager, rx, *_ = self.engine()
        manager.rx = rx
        manager.mode = "radio"
        manager.switching = True
        manager.select_view("radio")
        manager.select_view("log")
        manager.mode = "ft8"
        manager.switching = False
        with patch.object(manager, "switch") as switch:
            manager.tick()
        switch.assert_called_once_with("radio")

    def test_hidden_ft8_keeps_receive_setup_and_decoder_running(self):
        manager, rx, decoder, station, _ = self.engine()
        manager.rx = rx
        manager.mode = "ft8"
        manager.decoder = decoder
        manager.station = station
        manager.revision = 1
        with patch(
            "desktop_session.load_settings",
            return_value={"callsign": "K1ABC", "hide_worked": False},
        ):
            manager.select_view("log")
            manager.tick()
        self.assertTrue(manager.ready)
        decoder.activate.assert_called_once_with()
        self.assertEqual(manager.history.messages[0].text, "CQ W1AW FN31")
        station.action.assert_called_once_with("tune", dial=7074000)

    def test_reconnection_does_not_restart_automatic_tuner(self):
        manager, rx, decoder, station, _ = self.engine()
        manager.rx = rx
        manager.mode = "ft8"
        manager.decoder = decoder
        manager.station = station
        manager.revision = 1
        with patch(
            "desktop_session.load_settings",
            return_value={"callsign": "K1ABC", "hide_worked": False},
        ):
            manager.tick()
            station.action.reset_mock()
            rx.session_revision = 2
            manager.tick()
        self.assertFalse(
            any(c.args[0] == "tune" for c in station.action.call_args_list)
        )

    def test_non_ft8_frequency_requires_explicit_band_before_setup(self):
        manager, rx, decoder, station, _ = self.engine()
        manager.rx = rx
        manager.mode = "ft8"
        manager.decoder = decoder
        manager.station = station
        manager.revision = 1
        rx.frequency = 7100000
        with patch(
            "desktop_session.load_settings",
            return_value={"callsign": "K1ABC", "hide_worked": False},
        ):
            manager.tick()
        self.assertTrue(manager.band_required)
        self.assertFalse(manager.ready)
        rx.command.assert_not_called()
        station.action.assert_not_called()
        decoder.activate.assert_not_called()

    def test_handoff_waits_for_old_session_stop_before_new_start(self):
        manager, rx, _, _, _ = self.engine()
        events = []
        manager._stop = lambda: events.append("old stopped")
        rx.start.side_effect = lambda _: events.append("new started")
        manager.switching = True
        manager._transition("radio", ("fixture",))
        self.assertEqual(events, ["old stopped", "new started"])
        self.assertEqual(manager.mode, "radio")
        self.assertFalse(manager.switching)

    def test_failed_shutdown_never_opens_another_stream(self):
        manager, rx, *_ = self.engine()
        manager._stop = Mock(side_effect=RuntimeError("fixture"))
        manager._transition("ft8", ("fixture",))
        rx.start.assert_not_called()
        self.assertIn("RuntimeError", manager.error)

    def test_stop_and_fault_clear_remain_available_without_band(self):
        manager, rx, _, station, _ = self.engine()
        manager.rx = rx
        manager.station = station
        manager.mode = "ft8"
        manager.band_required = True
        manager.action("clear_fault")
        station.action.assert_called_once()
        with self.assertRaises(ValueError):
            manager.action("cq")


class SpectrumTests(unittest.TestCase):
    def test_data_renders_at_native_size_and_zoom_resets_scale(self):
        plot = Spectrum()
        bins = bytes([10] * 256 + [150] + [10] * 255)
        scope = (1, 7000000, 7080000, bins)
        self.assertTrue(plot.update(scope, 7040000, 1000, 220, 1))
        self.assertEqual(plot.image.size, (1000, 220))
        self.assertEqual(len(plot.levels), 1000)
        self.assertFalse(plot.update(scope, 7040000, 1000, 220, 1))
        self.assertTrue(plot.update(scope, 7040000, 1000, 220, 4))
        self.assertEqual(plot.edges, (7030000, 7050000))
        self.assertGreater(max(plot.levels), 100)
        self.assertEqual(plot.image.getpixel((500, 1)), (8, 15, 32))

    def test_zero_scope_and_edge_clamping(self):
        plot = Spectrum()
        self.assertFalse(plot.update(None, None, 1000, 200, 1))
        plot.update((1, 7000000, 7080000, bytes([20] * 512)), 7000000, 1200, 180, 8)
        self.assertEqual(plot.edges, (7000000, 7010000))


class NativeShortcuts(unittest.TestCase):
    def ui(self, view="radio"):
        from desktop_ui import DesktopApp

        ui = DesktopApp.__new__(DesktopApp)
        ui.held = {}
        ui.releases = {}
        ui.closing = False
        ui.current = lambda: view
        ui.root = Mock()
        ui.sessions = Mock()
        ui.sessions.rx = None
        ui.connect = Mock()
        ui.safe = lambda fn: fn()
        return ui

    def test_enter_on_native_button_invokes_it_instead_of_disconnecting(self):
        ui = self.ui()
        widget = Mock()
        widget.winfo_class.return_value = "TButton"
        event = SimpleNamespace(keycode=36, keysym="Return", state=0, widget=widget)
        self.assertEqual(ui.key_press(event), "break")
        widget.invoke.assert_called_once()
        ui.connect.assert_not_called()

    def test_ft8_enter_clears_fault_before_invoking_a_cq_button(self):
        ui = self.ui("ft8")
        ui.sessions.rx = SimpleNamespace(radio_state={"tx_fault": "SWR high"})
        widget = Mock()
        widget.winfo_class.return_value = "TButton"
        ui.key_press(
            SimpleNamespace(keycode=36, keysym="Return", state=0, widget=widget)
        )
        ui.sessions.action.assert_called_once_with("clear_fault")
        widget.invoke.assert_not_called()

    def test_native_entry_arrows_are_not_radio_tuning_commands(self):
        ui = self.ui()
        widget = Mock()
        widget.winfo_class.return_value = "TEntry"
        ui.tune = Mock()
        ui.key_press(
            SimpleNamespace(keycode=113, keysym="Left", state=0, widget=widget)
        )
        ui.tune.assert_not_called()
