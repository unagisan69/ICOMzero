import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from shutdown import stop_receiver
from desktop_ui import fresh_meter
from meters import Protection
from models import DEFAULT_PROFILE


class ShutdownTests(unittest.TestCase):
    def test_cancel_and_rx_request_precede_disconnect_and_join(self):
        events = []
        stop = threading.Event()
        release = threading.Event()

        def transport():
            stop.wait()
            events.append("transport cleanup")
            release.wait(2)
            events.append("logout complete")

        worker = threading.Thread(target=transport)
        rx = SimpleNamespace(
            stop=stop,
            thread=worker,
            tx_enabled=True,
            radio_state={"ptt": False},
            busy=worker.is_alive,
            stop_tx=lambda: events.append("cancel audio"),
            command=lambda name: events.append(name),
        )
        worker.start()
        closer = threading.Thread(target=lambda: stop_receiver(rx))
        closer.start()
        self.assertTrue(stop.wait(1))
        self.assertTrue(closer.is_alive(), "UI exit must wait for transport cleanup")
        release.set()
        closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertEqual(
            events,
            [
                "cancel audio",
                "force_rx",
                "tuner_stop",
                "transport cleanup",
                "logout complete",
            ],
        )

    def test_restore_failure_still_disconnects(self):
        rx = SimpleNamespace(
            stop=threading.Event(),
            thread=Mock(),
            radio_state={"ptt": False},
            busy=lambda: True,
            stop_tx=Mock(),
            command=Mock(),
        )
        with self.assertRaises(ValueError):
            stop_receiver(rx, before_disconnect=Mock(side_effect=ValueError()))
        self.assertTrue(rx.stop.is_set())
        rx.thread.join.assert_called_once_with()


class ReceiveOnlyStop(unittest.TestCase):
    def test_icom_can_unkey_but_cannot_key_in_radio_view(self):
        from civ import Civ

        civ = Civ.__new__(Civ)
        civ.tx_enabled = False
        civ.ptt_wanted = None
        civ.ptt(True)
        self.assertIsNone(civ.ptt_wanted)
        civ.ptt(False)
        self.assertIs(civ.ptt_wanted, False)

    def test_kenwood_can_unkey_but_cannot_key_in_radio_view(self):
        from kenwood import Cat

        cat = Cat.__new__(Cat)
        cat.tx_enabled = False
        cat.protection = SimpleNamespace(fault=None)
        cat.send, cat.on_state = Mock(), Mock()
        cat.ptt(True)
        cat.send.assert_not_called()
        cat.ptt(False)
        self.assertEqual([c.args[0] for c in cat.send.call_args_list], ["RX", "IF"])


class DesktopMeters(unittest.TestCase):
    def test_real_tx_reset_and_first_readback(self):
        state = {}
        protection = Protection(
            DEFAULT_PROFILE, lambda key, value: state.update({key: value})
        )
        protection.begin(now=100)
        for key in ("swr", "watts"):
            self.assertIsNone(fresh_meter(state, key, 100))
        protection.update("swr", 10, now=100.1)
        protection.update("watts", 100, now=100.1)
        self.assertIsNotNone(fresh_meter(state, "swr", 100.2))
        self.assertIsNotNone(fresh_meter(state, "watts", 100.2))
        self.assertIsNone(fresh_meter(state, "watts", 102))
        protection.begin(now=110)
        self.assertIsNone(fresh_meter(state, "watts", 110))

    def test_missing_invalid_future_or_stale_values_are_not_formatted(self):
        for stamp in (None, "bad", 200, 90):
            self.assertIsNone(fresh_meter({"swr_at": stamp, "swr": 1.1}, "swr", 100))
        for value in (None, "bad", float("nan")):
            self.assertIsNone(
                fresh_meter({"watts_at": 100, "watts_display": value}, "watts", 100)
            )
