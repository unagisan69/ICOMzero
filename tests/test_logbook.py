import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from contacts import manual_contact, imported_contact
from ft8log import DEFAULTS, Logbook, adif, save_settings
from ft8history import account_id, adif_records
from log_ui import LogScreens

SETTINGS = {
    **DEFAULTS,
    "callsign": "K1ABC",
    "grid": "FN42AB",
    "wavelog_url": "https://example.invalid",
    "wavelog_key": "fixture-only",
    "wavelog_station": "1",
    "wavelog_enabled": True,
    "qrz_key": "fixture-only",
    "qrz_enabled": True,
}
FORM = dict(
    call="W1AW",
    grid="FN31",
    date="2026-11-23",
    time="01:02:03",
    frequency="7.074",
    mode="FT8",
    sent="-10",
    received="-12",
    comment="Test",
)


def remote(call="W1AW", mode="FT8", stamp="010203", **extra):
    return dict(
        dict(
            CALL=call,
            STATION_CALLSIGN="K1ABC",
            MY_GRIDSQUARE="FN42AB",
            GRIDSQUARE="FN31",
            QSO_DATE="20261123",
            TIME_ON=stamp,
            TIME_OFF=stamp,
            FREQ="7.074",
            BAND="40m",
            MODE=mode,
            RST_SENT="-10",
            RST_RCVD="-12",
        ),
        **extra,
    )


def record(fields):
    return "".join(f"<{k}:{len(str(v))}>{v}" for k, v in fields.items()) + "<EOR>"


class ContactValidation(unittest.TestCase):
    def test_manual_modes_reports_and_adif(self):
        for mode, report in [
            ("FT8", "-10"),
            ("CW", "599"),
            ("SSB", "59"),
            ("FT4", "+04"),
        ]:
            q = manual_contact(
                {**FORM, "mode": mode, "sent": report, "received": report}, SETTINGS
            )
            self.assertEqual(q["band"], "40m")
            data = list(adif_records(adif(q), fields=None))[0]
            self.assertEqual(data["RST_SENT"], report)
            self.assertEqual(data.get("SUBMODE", data["MODE"]), mode)
        q = manual_contact(FORM, SETTINGS)
        q["sent"] = -10
        self.assertIn("<RST_SENT:3>-10", adif(q))

    def test_invalid_manual_entry_never_gets_contact_identity(self):
        for changes in (
            {"call": "NO-CALL"},
            {"date": "2026-02-30"},
            {"time": "25:00:00"},
            {"frequency": "NaN"},
            {"frequency": "7.074;bad"},
            {"grid": "ZZ99"},
            {"sent": "999"},
            {"call": "K1ABC"},
        ):
            with self.assertRaises(ValueError, msg=str(changes)):
                manual_contact({**FORM, **changes}, SETTINGS)
        with self.assertRaises(ValueError):
            imported_contact(remote(STATION_CALLSIGN="W2XYZ"), SETTINGS)

    def test_import_preserves_metadata_and_empty_reports(self):
        q = imported_contact(
            {
                **remote(mode="SSB"),
                "RST_SENT": "",
                "RST_RCVD": "",
                "COMMENT": "Portable",
            },
            SETTINGS,
        )
        self.assertEqual(q["mode"], "SSB")
        self.assertEqual(q["comment"], "Portable")
        self.assertEqual(q["imported_fields"]["STATION_CALLSIGN"], "K1ABC")
        self.assertNotIn("RST_SENT", list(adif_records(adif(q), fields=None))[0])


class FullLogSync(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.root.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Background upload workers remain disabled: only fixture transports are used.
        save_settings({**SETTINGS, "wavelog_enabled": False, "qrz_enabled": False})
        self.book = Logbook()
        self.addCleanup(self.book.close)

    def test_download_merge_queue_and_repeat_sync_are_idempotent(self):
        local = manual_contact(FORM, SETTINGS)
        self.book.add(local)
        extra = manual_contact({**FORM, "call": "W3XYZ"}, SETTINGS)
        self.book.add(extra)
        responses = [
            {"qsos": [remote(), remote("W2XYZ", "SSB")], "lastfetchedid": 10},
            {"adif": None, "lastfetchedid": 10},
        ]
        with patch("logsync.request", side_effect=responses) as http:
            self.book.contact_sync.sync_now("wavelog", SETTINGS)
        self.assertEqual(
            [c.args[1]["fetchfromid"] for c in http.call_args_list], [0, 10]
        )
        rows, count = self.book.page(0, 100)
        self.assertEqual(count, 3)
        self.assertEqual(self.book.delivery_states(local["id"])["wavelog"], "sent")
        self.assertEqual(self.book.delivery_states(extra["id"])["wavelog"], "pending")
        self.assertIn("W1AW", self.book.worked_calls("40m", SETTINGS))
        self.assertNotIn("W2XYZ", self.book.worked_calls("40m", SETTINGS))
        with patch(
            "logsync.request", return_value={"adif": None, "lastfetchedid": 10}
        ) as http:
            self.book.contact_sync.sync_now("wavelog", SETTINGS)
        self.assertEqual(http.call_args.args[1]["fetchfromid"], 10)
        self.assertEqual(self.book.page()[1], 3)
        self.assertEqual(len(self.book.recent()), 3)

    def test_qrz_pages_keep_all_contact_fields_and_advance_ids(self):
        a = remote(APP_QRZLOG_LOGID="10")
        b = remote("W2XYZ", "CW", APP_QRZLOG_LOGID="11")
        c = remote("W3XYZ", "SSB", APP_QRZLOG_LOGID="12")
        with patch("logsync.PAGE_SIZE", 2), patch(
            "logsync.request",
            side_effect=[
                {"RESULT": "OK", "ADIF": record(a) + record(b)},
                {"RESULT": "OK", "ADIF": record(c)},
            ],
        ) as http:
            self.book.contact_sync.sync_now("qrz", SETTINGS)
        self.assertIn("AFTERLOGID:12", http.call_args.args[1]["OPTION"])
        rows, total = self.book.page(0, 10)
        self.assertEqual(total, 3)
        self.assertEqual({q["mode"] for q in rows}, {"FT8", "CW", "SSB"})
        self.assertTrue(
            all(self.book.delivery_states(q["id"])["qrz"] == "sent" for q in rows)
        )

    def test_qrz_empty_page_is_complete_but_auth_failure_is_not(self):
        with patch("logsync.request", return_value={"RESULT": "FAIL", "COUNT": "0"}):
            self.book.contact_sync.sync_now("qrz", SETTINGS)
        self.assertIn("0 new", self.book.contact_sync.status)
        with patch(
            "logsync.request",
            return_value={"RESULT": "FAIL", "COUNT": "0", "REASON": "invalid API key"},
        ):
            self.book.contact_sync.run("qrz", SETTINGS)
        self.assertIn("failed", self.book.contact_sync.status)

    def test_import_without_frequency_preserves_unknown_instead_of_guessing(self):
        fields = {**remote(), "FREQ": ""}
        q = imported_contact(fields, SETTINGS)
        self.assertEqual(q["dial"], 0)
        self.assertNotIn("FREQ", list(adif_records(adif(q), fields=None))[0])
        self.book.contact_sync.apply_page(
            "wavelog", SETTINGS, account_id("wavelog", SETTINGS), [fields], 1
        )
        self.assertEqual(self.book.page()[1], 1)

    def test_pending_uploads_stay_bound_to_original_account(self):
        # Newly saved contacts bind their delivery to the account in effect then.
        save_settings(SETTINGS)
        with patch("ft8log.request", return_value={"status": "created"}) as upload:
            q = manual_contact(FORM, SETTINGS)
            # Keep the worker disabled until after inspecting its account binding.
            self.book.closed.set()
            self.book.add(q)
        with self.book.db() as db:
            bound = db.execute(
                "SELECT account FROM delivery_accounts WHERE id=? AND service='wavelog'",
                (q["id"],),
            ).fetchone()[0]
        self.assertEqual(bound, account_id("wavelog", SETTINGS))
        upload.assert_not_called()

    def test_failed_page_resumes_at_committed_cursor_without_losing_contacts(self):
        with patch(
            "logsync.request",
            side_effect=[{"qsos": [remote()], "lastfetchedid": 42}, OSError("fixture")],
        ):
            with self.assertRaises(OSError):
                self.book.contact_sync.sync_now("wavelog", SETTINGS)
        self.assertEqual(self.book.page()[1], 1)
        with patch(
            "logsync.request", return_value={"adif": None, "lastfetchedid": 42}
        ) as http:
            self.book.contact_sync.sync_now("wavelog", SETTINGS)
        self.assertEqual(http.call_args.args[1]["fetchfromid"], 42)
        self.assertEqual(self.book.page()[1], 1)

    def test_bad_or_other_station_records_are_skipped_and_reported(self):
        records = [
            {**remote(), "STATION_CALLSIGN": "W2XYZ"},
            {**remote(), "QSO_DATE": "broken"},
            remote(),
        ]
        with patch(
            "logsync.request",
            side_effect=[
                {"qsos": records, "lastfetchedid": 3},
                {"adif": None, "lastfetchedid": 3},
            ],
        ):
            self.book.contact_sync.sync_now("wavelog", SETTINGS)
        self.assertEqual(self.book.page()[1], 1)
        self.assertIn("2 skipped", self.book.contact_sync.status)

    def test_changing_account_does_not_reuse_another_accounts_sent_markers(self):
        q = manual_contact(FORM, SETTINGS)
        self.book.add(q)
        self.book.contact_sync.apply_page(
            "wavelog", SETTINGS, account_id("wavelog", SETTINGS), [remote()], 1
        )
        self.assertEqual(self.book.delivery_states(q["id"])["wavelog"], "sent")
        changed = {**SETTINGS, "wavelog_key": "different-fixture-account"}
        self.book.contact_sync.queue_missing("wavelog", changed)
        self.assertEqual(self.book.delivery_states(q["id"])["wavelog"], "pending")

    def test_search_paging_and_duplicate_outside_recent_window(self):
        q = manual_contact(FORM, SETTINGS)
        self.book.add(q)
        with self.book.db() as db:
            for i in range(120):
                other = {
                    **q,
                    "id": f"fixture-{i}",
                    "call": f"W{i}XYZ",
                    "start": q["start"] + i + 1,
                    "end": q["end"] + i + 1,
                }
                db.execute(
                    "INSERT INTO qsos VALUES (?,?,?)",
                    (other["id"], json.dumps(other), adif(other)),
                )
        self.assertFalse(self.book.add(q))
        rows, count = self.book.page(4, 4, "XYZ")
        self.assertEqual(count, 120)
        self.assertEqual(len(rows), 4)
        self.assertGreater(rows[0]["start"], rows[-1]["start"])
        self.assertEqual(self.book.page(0, 4, "W1AW")[1], 1)
        self.assertEqual(self.book.page(0, 4, "' OR 1=1")[1], 0)

    def test_error_status_never_exposes_credentials(self):
        with patch("logsync.request", side_effect=OSError("secret URL and key")):
            self.book.contact_sync.run("wavelog", SETTINGS)
        self.assertNotIn("secret", self.book.contact_sync.status)
        self.assertIn("OSError", self.book.contact_sync.status)
        self.assertFalse(self.book.contact_sync.busy)


class ManualEntryUI(unittest.TestCase):
    def test_save_uses_shared_log_and_cancel_saves_nothing(self):
        screen = LogScreens.__new__(LogScreens)
        screen.book = Mock()
        screen.message = Mock()
        screen.logging = Mock()
        choices = iter([0, 9])
        screen.select = lambda *_a, **_k: next(choices)
        screen.input = lambda *_a, **_k: "W1AW"
        with patch("log_ui.load_settings", return_value=SETTINGS):
            self.assertTrue(screen.new_entry())
        q = screen.book.add.call_args.args[0]
        self.assertEqual(q["call"], "W1AW")
        self.assertEqual(q["source"], "manual")
        screen.book.reset_mock()
        screen.select = lambda *_a, **_k: None
        with patch("log_ui.load_settings", return_value=SETTINGS):
            self.assertFalse(screen.new_entry())
        screen.book.add.assert_not_called()
