from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from countries import country, message_country
from ft8 import Message
from us_states import state_for_call, label, database_paths


class StateTests(unittest.TestCase):
    def tearDown(self):
        state_for_call.cache_clear()

    def test_exact_fcc_state_not_call_area_guess(self):
        self.assertEqual(state_for_call("W1AW"), "CT")
        self.assertEqual(state_for_call("K1JT"), "NJ")
        self.assertEqual(
            message_country(Message(100, 1500, "CQ W1AW FN31", -12)), "USA-CT"
        )
        self.assertEqual(country("W1AW"), "United States")

    def test_portable_foreign_prefix_retains_country(self):
        self.assertEqual(
            message_country(Message(100, 1500, "CQ EA8/W1AW IL18", -12)),
            "Canary Islands",
        )
        self.assertEqual(label("W1AW/P", "United States"), "USA-CT")
        self.assertEqual(label("KH6UNLISTED", "Hawaii"), "USA-HI")
        self.assertEqual(label("K4UNLISTED", "United States"), "USA-??")

    def test_packaged_table_contains_no_names_addresses_or_contacts(self):
        with sqlite3.connect(database_paths()[-1]) as db:
            self.assertEqual(
                [r[1] for r in db.execute("PRAGMA table_info(states)")],
                ["call", "state"],
            )
            self.assertGreater(
                db.execute("SELECT count(*) FROM states").fetchone()[0], 800000
            )
            self.assertEqual(
                dict(db.execute("SELECT * FROM metadata"))["snapshot"], "2026-11-15"
            )

    def test_invalid_override_falls_back_to_bundled_database(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ):
            p = Path(root) / "icomzero/us-states.sqlite3"
            p.parent.mkdir()
            p.write_bytes(b"not SQLite")
            state_for_call.cache_clear()
            self.assertEqual(state_for_call("W1AW"), "CT")
