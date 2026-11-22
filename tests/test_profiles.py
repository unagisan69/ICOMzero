import json
import os
from pathlib import Path
import tempfile
import unittest
from profiles import ProfileStore, FIELDS


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private" / "radio.json"
        self.store = ProfileStore(self.path)

    def test_legacy_migration_and_clone_do_not_share_mutable_settings(self):
        self.path.parent.mkdir()
        original = {
            "Host": "192.0.2.10",
            "Username": "test",
            "Password": "private-test",
            "Radio": "IC-7300MK2",
            "volume": 35,
            "muted": True,
        }
        self.path.write_text(json.dumps(original))
        local = self.store.active()["id"]
        self.assertEqual(json.loads(self.path.read_text()), original)
        self.store.update(local, name="Local")
        remote = self.store.add(local)
        self.store.update(
            remote, name="Remote", connection={"Host": "203.0.113.10"}, step=100
        )
        self.assertEqual(self.store.active()["id"], local)
        self.store.select(remote)
        self.assertEqual(self.store.config()["Host"], "203.0.113.10")
        self.assertEqual(self.store.config()["Password"], "private-test")
        self.store.save_connection(
            [
                "203.0.113.11",
                "50001",
                "new-user",
                "new-pass",
                "IC-7300MK2",
                "60001",
                "User",
            ],
            45,
            False,
        )
        self.store.select(local)
        config = self.store.config()
        self.assertEqual(config["Host"], "192.0.2.10")
        self.assertEqual(config["Password"], "private-test")
        self.assertEqual(config["step"], 1000)
        self.assertEqual(config["volume"], 45)
        self.assertFalse(config["muted"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Password", json.loads(self.path.read_text()))

    def test_bad_name_or_step_keeps_file_unchanged_and_delete_reselects(self):
        local = self.store.active()["id"]
        self.store.update(local, name="Local")
        other = self.store.add(local)
        before = self.path.read_bytes()
        for changes in ({"name": "local"}, {"name": ""}, {"step": 250}):
            with self.assertRaises(ValueError):
                self.store.update(other, **changes)
            self.assertEqual(self.path.read_bytes(), before)
        self.store.select(other)
        self.store.delete(other)
        self.assertEqual(self.store.active()["id"], local)
        with self.assertRaises(ValueError):
            self.store.delete(local)

    def test_corrupt_settings_are_not_replaced_with_empty_defaults(self):
        self.path.parent.mkdir()
        self.path.write_text("{bad json")
        with self.assertRaises(ValueError):
            self.store.save_connection([""] * len(FIELDS))
        self.assertEqual(self.path.read_text(), "{bad json")
