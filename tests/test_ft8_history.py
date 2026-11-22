import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ft8history import WorkedHistory, SyncError, account_id, adif_records
from ft8log import DEFAULTS
from ft8 import History, Message


class WorkedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "worked.sqlite3"
        self.book = WorkedHistory(path=self.path, start=False, pause=0)
        self.settings = {
            **DEFAULTS,
            "callsign": "K4TTL",
            "grid": "EM85RN",
            "wavelog_key": "test-only",
            "wavelog_url": "https://example.invalid",
            "wavelog_station": "1",
            "qrz_key": "test-only",
        }

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def test_only_ft8_current_band_and_configured_account(self):
        account = account_id("wavelog", self.settings)
        self.book.apply(
            account,
            [
                {"CALL": "W1AW", "BAND": "40m", "MODE": "FT8"},
                {"CALL": "W2AW", "BAND": "20m", "MODE": "MFSK", "SUBMODE": "FT8"},
                {"CALL": "W3AW", "BAND": "40m", "MODE": "SSB"},
            ],
            3,
            True,
        )
        self.assertEqual(self.book.calls(self.settings, "40m"), {"W1AW"})
        self.assertEqual(self.book.calls(self.settings, "20m"), {"W2AW"})
        self.assertEqual(
            self.book.calls({**self.settings, "wavelog_key": "another"}, "40m"), set()
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with self.book.db() as db:
            self.assertEqual(
                {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
                {"worked", "sync_state"},
            )
        restored = WorkedHistory(path=self.path, start=False)
        self.assertEqual(restored.calls(self.settings, "40m"), {"W1AW"})
        restored.close()

    def test_incremental_wavelog_follows_cursor_despite_deduplicated_short_page(self):
        responses = [
            {
                "qsos": [{"CALL": "W1AW", "BAND": "40m", "MODE": "FT8"}],
                "lastfetchedid": 250,
            },
            {
                "qsos": [{"CALL": "W2AW", "BAND": "40m", "MODE": "FT8"}],
                "lastfetchedid": 251,
            },
            {"adif": None, "lastfetchedid": 251},
        ]
        with patch("ft8history.request", side_effect=responses) as http:
            self.book.sync_now("wavelog", self.settings)
        self.assertEqual(
            [c.args[1]["fetchfromid"] for c in http.call_args_list], [0, 250, 251]
        )
        self.assertEqual(self.book.calls(self.settings, "40m"), {"W1AW", "W2AW"})
        with patch(
            "ft8history.request", return_value={"adif": None, "lastfetchedid": 251}
        ) as http:
            self.book.sync_now("wavelog", self.settings)
            self.assertEqual(http.call_args.args[1]["fetchfromid"], 251)

    def test_failure_keeps_committed_page_and_resumable_cursor(self):
        account = account_id("wavelog", self.settings)
        with patch(
            "ft8history.request",
            side_effect=[
                {
                    "qsos": [{"CALL": "W1AW", "BAND": "40m", "MODE": "FT8"}],
                    "lastfetchedid": 12,
                },
                TimeoutError("secret body"),
            ],
        ):
            with self.assertRaises(TimeoutError):
                self.book.sync_now("wavelog", self.settings)
        self.assertEqual(self.book.cursor(account), 12)
        self.assertEqual(self.book.calls(self.settings, "40m"), {"W1AW"})
        with patch(
            "ft8history.request",
            return_value={"qsos": [{"CALL": "W2AW"}], "lastfetchedid": 12},
        ):
            with self.assertRaises(SyncError):
                self.book.sync_now("wavelog", self.settings)
        self.assertEqual(self.book.cursor(account), 12)

    def test_qrz_parses_adif_and_resumes_after_last_logid(self):
        adif = "<CALL:4>W1AW<BAND:3>40m<MODE:3>FT8<APP_QRZLOG_LOGID:2>17<EOR>"
        with patch(
            "ft8history.request",
            return_value={"RESULT": "OK", "COUNT": "999", "ADIF": adif},
        ):
            self.book.sync_now("qrz", self.settings)
        self.assertEqual(self.book.cursor(account_id("qrz", self.settings)), 18)
        self.assertEqual(self.book.calls(self.settings, "40m"), {"W1AW"})
        with self.assertRaises(SyncError):
            list(adif_records("<CALL:9>W1AW"))

    def test_history_hides_worked_but_keeps_active_partner_and_own_tx(self):
        messages = [
            Message(1, 1500, "CQ W1AW FN31", -5),
            Message(2, 1500, "K4TTL W1AW -10", -7),
            Message(3, 1500, "CQ W2AW FN31", -8),
            Message(4, 1500, "W1AW K4TTL RRR", 0, True),
        ]
        history = History()
        history.update(messages, "K4TTL", frozenset({"W1AW"}))
        self.assertEqual(history.messages, messages[2:])
        history.update(messages, "K4TTL", frozenset({"W1AW"}), "W1AW")
        self.assertEqual(history.messages, messages)
        history.update(messages, "K4TTL")
        self.assertEqual(history.messages, messages)

    def test_automation_excludes_worked_cqs_but_queues_direct_callers(self):
        from ft8auto import AutoCQ
        from ft8qso import Sequencer

        with patch("time.time", return_value=1500):
            seq = Sequencer(self.settings, lambda _: None)
            auto = AutoCQ(seq, lambda *a, **kw: None, lambda call: call == "W1AW")
            auto.start(7074000)
            auto.receive(Message(100, 1500, "CQ W1AW FN31", -5))
            auto.receive(Message(100, 1500, "K4TTL W1AW FN31", -5))
            self.assertNotIn("W1AW", auto.cqs)
            self.assertIn("W1AW", auto.queue)
            self.assertTrue(auto._queued(1500))
            self.assertEqual(seq.partner, "W1AW")
            self.assertFalse(
                auto._target(Message(100, 1500, "CQ W1AW FN31", -5), "fallback")
            )
            auto.start(7074000, mode="pota")
            auto.receive(Message(100, 1500, "CQ POTA W1AW FN31", -5))
            auto.prepare()
            self.assertEqual(seq.stage, "monitor")
