import os
import runpy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from ft8 import Message, History
from ft8qso import Sequencer, contact_draft, finalize_contact, tx_frequency
from ft8log import DEFAULTS, Logbook, save_settings, validate_station
from ft8station import Station
from ft8tx import Job
from meters import Protection
from models import DEFAULT_PROFILE

SETTINGS = {**DEFAULTS, "callsign": "K1ABC", "grid": "FN42AB"}


class Replies(unittest.TestCase):
    def make(self, extra, slot=101, settings=None, history=()):
        seq = Sequencer(settings or SETTINGS, lambda _: None)
        message = Message(slot, 1934, "K1ABC W1AW " + extra, -12)
        with patch("time.time", return_value=slot * 15 + 0.2):
            seq.reply(message, 7074000, history)
        return seq

    def test_reply_stage_matches_grid_report_rreport_and_ack(self):
        for extra, stage, tail in [
            ("FN31", "report", "-12"),
            ("-10", "rreport", "R-12"),
            ("R-10", "rrr", "RRR"),
            ("RR73", "73", "73"),
            ("73", "73", "73"),
        ]:
            seq = self.make(extra)
            self.assertEqual(seq.stage, stage)
            self.assertEqual(seq.message, "W1AW K1ABC " + tail)

    def test_old_highlight_uses_latest_addressed_stage_and_actual_sent_report(self):
        cq = Message(99, 1700, "CQ W1AW FN31", -15)
        sent = Message(100, 1700, "W1AW K1ABC -12", 0, local_tx=True)
        ack = Message(101, 1934, "K1ABC W1AW R-10", -8)
        seq = Sequencer(SETTINGS, lambda _: None)
        with patch("time.time", return_value=1515.2):
            seq.reply(cq, 7074000, [cq, sent, ack])
        self.assertEqual(seq.message, "W1AW K1ABC RRR")
        self.assertEqual((seq.sent, seq.received), (-12, -10))
        self.assertEqual(seq.hz, 1934)
        self.assertEqual(seq.parity, 0)

    def test_resume_keeps_contact_identity_and_reports(self):
        seq = Sequencer(SETTINGS, lambda _: None)
        seq.partner = "W1AW"
        seq.dial = 7074000
        seq.start = 1500.1
        seq.qso_start = 1500.5
        seq.sent = -7
        seq.grid = "FN31"
        seq.last_slot = 100
        with patch("time.time", return_value=1515.2):
            seq.reply(Message(101, 900, "K1ABC W1AW R-10", -12), 7074000)
        self.assertEqual(seq.start, 1500.1)
        self.assertEqual(seq.sent, -7)
        self.assertEqual(seq.qso_start, 1500.5)
        self.assertEqual(seq.message, "W1AW K1ABC RRR")

    def test_both_received_parities_choose_opposite_period(self):
        for slot in (100, 101):
            seq = self.make("FN31", slot)
            self.assertIsNone(seq.next(slot * 15 + 0.1))
            next_slot = slot + 1
            self.assertEqual(seq.next(next_slot * 15 + 0.1)[3], next_slot)
            self.assertIsNone(seq.next(next_slot * 15 + 0.15))

    def test_auto_default_follows_partner_and_manual_value_is_retained(self):
        self.assertEqual(DEFAULTS["tx_hz"], "Auto")
        validate_station(SETTINGS)
        self.assertEqual(tx_frequency(SETTINGS), 1500)
        self.assertEqual(self.make("FN31").hz, 1934)
        self.assertEqual(
            self.make("FN31", settings={**SETTINGS, "tx_hz": "1700"}).hz, 1700
        )

    def test_own_cq_adopts_first_responder_offset(self):
        seq = Sequencer(SETTINGS, lambda _: None)
        with patch("time.time", return_value=1500.1):
            seq.cq(7074000)
        pending = seq.next(1500.1)
        seq.transmitted(pending, True)
        seq.receive(Message(101, 850, "K1ABC W1AW FN31", -10))
        self.assertEqual(seq.hz, 850)
        self.assertEqual(seq.parity, 0)

    def test_unknown_prior_report_is_not_invented_for_auto_logging(self):
        seq = self.make("R-10")
        self.assertIsNone(seq.sent)
        self.assertEqual(seq.received, -10)
        seq.final_seen = True
        seq.finish()
        self.assertEqual(seq.status, "Incomplete contact; not logged")

    def test_new_cq_does_not_reuse_completed_contact_reports(self):
        seq = Sequencer(SETTINGS, lambda _: None)
        seq.partner = "W1AW"
        seq.dial = 7074000
        seq.start = 1500
        seq.received = -10
        seq.sent = -12
        seq.qso_start = 1500.5
        cq = Message(104, 1800, "CQ W1AW FN31", -8)
        with patch("time.time", return_value=1560.2):
            seq.reply(cq, 7074000)
        self.assertIsNone(seq.received)
        self.assertIsNone(seq.qso_start)
        self.assertEqual(seq.start, 1560.2)


class ManualLogging(unittest.TestCase):
    def draft(self):
        rows = [
            Message(100, 1500, "W1AW K1ABC -12", 0, True),
            Message(101, 1500, "K1ABC W1AW R-10", -8),
            Message(102, 1500, "W1AW K1ABC RRR", 0, True),
        ]
        return contact_draft(SETTINGS, rows, rows[1], 7074000)

    def test_manual_recovery_uses_exchanged_reports_without_terminal_73(self):
        q = finalize_contact(self.draft())
        self.assertEqual((q["call"], q["sent"], q["received"]), ("W1AW", -12, -10))
        self.assertEqual(q["band"], "40m")
        self.assertTrue(q["id"])

    def test_missing_reports_require_operator_entry(self):
        cq = Message(100, 1500, "CQ W1AW FN31", -12)
        draft = contact_draft(SETTINGS, [cq], cq, 7074000)
        with self.assertRaisesRegex(ValueError, "both exchanged"):
            finalize_contact(draft)
        draft.update(sent="-12", received="-10")
        self.assertEqual(finalize_contact(draft)["received"], -10)

    def test_overlapping_manual_recovery_never_duplicates_or_requeues_qso(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ):
            save_settings(SETTINGS)
            book = Logbook()
            book.close()
            q = finalize_contact(self.draft())
            self.assertTrue(book.add(q))
            recovery = {**q, "id": "manual-recovery", "start": q["start"] + 15}
            self.assertFalse(book.add(recovery))
            self.assertEqual(len(book.recent()), 1)
            new = {
                **q,
                "id": "later-contact",
                "start": q["end"] + 30,
                "end": q["end"] + 60,
            }
            self.assertTrue(book.add(new))

    def test_later_cq_separates_a_new_contact_from_old_reports(self):
        old = [
            Message(100, 1500, "W1AW K1ABC -12", 0, True),
            Message(101, 1500, "K1ABC W1AW R-10", -8),
        ]
        cq = Message(104, 1800, "CQ W1AW FN31", -8)
        draft = contact_draft(SETTINGS, old + [cq], cq, 7074000)
        self.assertIsNone(draft["sent"])
        self.assertIsNone(draft["received"])

    def test_rr73_acknowledgment_is_not_recorded_as_a_grid(self):
        rows = [
            Message(100, 1500, "W1AW K1ABC -12", 0, True),
            Message(101, 1500, "K1ABC W1AW R-10", -8),
            Message(103, 1500, "K1ABC W1AW RR73", -8),
        ]
        draft = contact_draft(SETTINGS, rows, rows[-1], 7074000)
        self.assertEqual(draft["grid"], "")
        self.assertEqual(finalize_contact(draft)["received"], -10)

    def test_manual_record_requires_own_station_identity(self):
        draft = self.draft()
        draft["own_call"] = ""
        with self.assertRaisesRegex(ValueError, "callsign"):
            finalize_contact(draft)

    def test_notification_occurs_only_after_new_local_save(self):
        s = Station.__new__(Station)
        s.logbook = Mock()
        s.qso_revision = 0
        s.saved_qso = None
        q = finalize_contact(self.draft())
        s.logbook.add.return_value = False
        self.assertFalse(s.complete_contact(q))
        self.assertEqual(s.qso_revision, 0)
        s.logbook.add.return_value = True
        self.assertTrue(s.complete_contact(q))
        self.assertEqual(s.qso_revision, 1)
        self.assertEqual(s.saved_qso["call"], "W1AW")


class DelayedTxRows(unittest.TestCase):
    def test_local_row_waits_for_end_of_15_second_period(self):
        real_mono = time.monotonic
        clock = [1500.1, 10.1]
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

        with patch("time.time", side_effect=lambda: clock[0]), patch(
            "time.monotonic", side_effect=lambda: clock[1]
        ), patch("ft8station.clock_health", return_value=(True, "Synced")), patch(
            "ft8station.load_settings", return_value=SETTINGS
        ), patch(
            "ft8station.waveform", return_value=bytes(96000)
        ):
            station = Station(receiver, decoder, Mock())
            try:
                station.action("cq")
                wait(lambda: bool(jobs))
                clock[:] = [1500.5, 10.5]
                jobs[0].started.set()
                wait(lambda: getattr(jobs[0], "listed", False))
                decoder.record_tx.assert_not_called()
                clock[:] = [1513.3, 23.3]
                jobs[0].success = True
                jobs[0].done.set()
                time.sleep(0.06)
                decoder.record_tx.assert_not_called()
                clock[:] = [1515, 25]
                wait(lambda: decoder.record_tx.called)
                decoder.record_tx.assert_called_once_with(100, 1500, "CQ K1ABC FN42")
            finally:
                station.close()


class PowerAndDisplay(unittest.TestCase):
    def test_display_smoothing_preserves_raw_spikes_and_swr_cutoff(self):
        state = {}
        p = Protection(DEFAULT_PROFILE, state.__setitem__)
        p.begin(now=10)
        p.update("watts", 213, 10.1)
        p.update("watts", 213, 10.35)
        p.update("watts", 0, 10.6)
        self.assertEqual(state["watts"], 0)
        self.assertEqual(state["watts_display"], 100)
        p.update("swr", 81, 10.6)
        self.assertIn("SWR", p.check(10.6))

    def test_raw_steady_power_summary_includes_real_mid_frame_dips(self):
        state = {}
        p = Protection(DEFAULT_PROFILE, state.__setitem__)
        p.begin(now=10)
        for t, v in [(10.1, 0), (11, 213), (12, 0), (13, 213), (14.7, 0)]:
            p.update("watts", v, t)
        with patch("time.monotonic", return_value=15):
            p.end()
        self.assertEqual(state["power_summary"]["min_w"], 0)
        self.assertEqual(state["power_summary"]["max_w"], 100)
        self.assertEqual(state["power_summary"]["samples"], 3)

    def test_tx_progress_red_and_arm_is_red_dot(self):
        from PIL import ImageFont
        from types import SimpleNamespace

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        display = SimpleNamespace(
            fonts={
                k: ImageFont.truetype(font, n)
                for k, n in [("small", 12), ("body", 14), ("title", 22)]
            }
        )
        image = module["ft8_frame"](
            display, History(), "K1ABC", True, "", 0.5, transmitting=True, armed=True, auto_active=True
        )
        self.assertEqual(image.getpixel((30, 147)), (240, 100, 100))
        self.assertEqual(image.getpixel((18, 161)), (240, 100, 100))
        popup = module["qso_popup"](display, image, {"call": "W1AW", "band": "40m"})
        self.assertEqual(popup.getpixel((25, 80)), (18, 63, 48))


class SelectedMenus(unittest.TestCase):
    def run_menu(self, manual=False, auto=False, pota=False, fault=False, initial=7074000, choose=False, sync=False):
        from PIL import ImageFont

        module = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "app/icomzero")
        )
        receiver = module["Receiver"]()
        actions = []
        tunes = []
        receiver.tune = lambda hz=None, **kw: tunes.append(hz)

        def start(_):
            receiver.frequency = initial
            receiver.radio_state = {"profile": DEFAULT_PROFILE, "mode": 1, "data_mode": 1, "split": 0, "rit": 0, "xit": 0, "nr": False, "tx_fault": "SWR telemetry lost — TX stopped" if fault else None}

        receiver.start = start
        receiver.busy = lambda: not receiver.stop.is_set()

        class Display:
            def __init__(self, _):
                font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                self.fonts = {
                    k: ImageFont.truetype(font, n)
                    for k, n in [("small", 12), ("body", 14), ("title", 22)]
                }

            def present(self, _):
                pass

        rows = [
            Message(99, 1500, "CQ W1AW FN31", -10),
            Message(100, 1500, "W1AW K1ABC -12", 0, True),
            Message(101, 1934, "K1ABC W1AW R-10", -8),
        ]

        class Decoder:
            def __init__(self, rate=48000):
                assert rate == 12000
                self.feed = lambda _: None

            def snapshot(self):
                return rows, "Ready", 0.5

            def activate(self, *_, **__):
                pass

            def close(self):
                pass

        class Book:
            status = "Ready"
            history_status = "Worked history ready"

            def sync_history(self, service, settings):
                actions.append((("sync_history", service), {}))

            def close(self):
                pass

        class Controller:
            status = "Receive only"
            clock_status = "Synced"
            active = False
            tuning = False
            qso_revision = 0

            def __init__(self, *_):
                pass

            def action(self, *args, **kwargs):
                actions.append((args, kwargs))

            def close(self):
                pass

        keys = [33, 15] + ([45] * 7 if manual else [45] * (3 if pota else (2 if auto else 1))) + [28]
        if sync:
            keys = [15] + [45] * 4 + [28] + [45] * 8 + [28, 28, 1, 1, 1]
        if initial not in (7074000, 14074000):
            keys = [28, 1] if choose else [15, 1]
        if manual:
            keys += [45] * 4 + [28]
        keys += [1, 1] if fault else [1]
        read, write = os.pipe()
        try:
            os.write(
                write,
                b"".join(
                    module["EVENT"].pack(0, 0, 1, key, value)
                    for key in keys
                    for value in (1, 0)
                ),
            )
            with patch.dict(
                module["ft8_ui"].__globals__,
                {
                    "Display": Display,
                    "Receiver": lambda: receiver,
                    "load": lambda: ["192.0.2.1", "50001", "user", "password"],
                },
            ), patch("ft8.Decoder", Decoder), patch("ft8log.Logbook", Book), patch(
                "ft8log.load_settings", return_value=SETTINGS
            ), patch(
                "ft8station.Station", Controller
            ):
                module["ft8_ui"](-1, read)
        finally:
            os.close(read)
            os.close(write)
        self.last_tunes = tunes
        return actions, rows

    def test_settings_sync_menu_downloads_selected_provider(self):
        actions, _ = self.run_menu(sync=True)
        self.assertIn((("sync_history", "wavelog"), {}), actions)

    def test_selected_call_cq_routes_to_reply_not_fresh_cq(self):
        actions, rows = self.run_menu()
        reply = next(args for args, _ in actions if args[0] == "reply")
        self.assertEqual(reply[1], rows[-1])
        self.assertFalse(any(args[0] == "cq" for args, _ in actions))

    def test_highlight_record_menu_reviews_then_submits_manual_qso(self):
        actions, _ = self.run_menu(manual=True)
        record = next(args for args, _ in actions if args[0] == "record_qso")
        self.assertEqual(
            (record[1]["call"], record[1]["sent"], record[1]["received"]),
            ("W1AW", -12, -10),
        )
        self.assertFalse(any(args[0] in ("cq", "reply") for args, _ in actions))

    def test_auto_cq_beneath_call_cq_ignores_highlight(self):
        actions, _ = self.run_menu(auto=True)
        self.assertTrue(any(args[0] == "auto_cq" for args, _ in actions))
        self.assertFalse(any(args[0] in ("cq", "reply") for args, _ in actions))

    def test_enter_clears_rf_warning_without_answering_selected_decode(self):
        actions, _ = self.run_menu(fault=True)
        self.assertTrue(any(args[0] == "clear_fault" for args, _ in actions))
        self.assertFalse(any(args[0] in ("cq", "reply", "auto_cq", "pota_hunt") for args, _ in actions))

    def test_pota_hunt_menu_action_is_beneath_auto_cq(self):
        actions, _ = self.run_menu(pota=True)
        self.assertTrue(any(args[0] == "pota_hunt" for args, _ in actions))

    def test_existing_20m_frequency_is_not_silently_retuned_to_40m(self):
        actions, _ = self.run_menu(auto=True, initial=14074000)
        auto = next((kw for args,kw in actions if args[0]=="auto_cq"),None)
        self.assertEqual(auto['dial'],14074000)
        self.assertNotIn(7074000,self.last_tunes)

    def test_non_ft8_frequency_requires_selection_and_tab_cannot_bypass(self):
        actions, _ = self.run_menu(initial=14200000)
        self.assertFalse(any(args[0] in ('tune','cq','reply','auto_cq','pota_hunt') for args,_ in actions))
        self.assertFalse(self.last_tunes)

    def test_band_choice_sets_designated_frequency_before_beginning(self):
        self.run_menu(initial=14200000, choose=True)
        self.assertIn(14074000,self.last_tunes)
