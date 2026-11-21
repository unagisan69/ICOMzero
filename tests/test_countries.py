import unittest
from countries import country, database, message_country, activation_cq
from ft8 import Message


class CountriesTests(unittest.TestCase):
    def test_global_entities_and_longest_prefix(self):
        for call, expected in [
            ("K4TTL", "United States"),
            ("VE3ABC", "Canada"),
            ("JA1ABC", "Japan"),
            ("VK2ABC", "Australia"),
            ("KH6ABC", "Hawaii"),
            ("EA8/K4TTL", "Canary Islands"),
            ("K4TTL/VE3", "Canada"),
            ("K4TTL/P", "United States"),
        ]:
            self.assertEqual(country(call), expected, call)
        self.assertGreater(len(database().prefix), 6000)
        self.assertGreater(len(database().exact), 20000)

    def test_full_call_exceptions_override_home_prefix(self):
        db = database()
        call = next(
            c
            for c, entity in db.exact.items()
            if "/" not in c and db.prefix_country(c) != entity and c != "VERSION"
        )
        self.assertEqual(country(call), db.exact[call])
        self.assertNotEqual(country(call), db.prefix_country(call))

    def test_mobile_hashed_and_unknown(self):
        self.assertEqual(country("K4TTL/MM"), "Maritime mobile")
        self.assertEqual(country("K4TTL/AM"), "Aeronautical mobile")
        self.assertEqual(country("<...>"), "Unknown")
        self.assertEqual(country("0QQQQ"), "Unknown")

    def test_activation_tags_and_message_sender(self):
        for tag in ("POTA", "SOTA"):
            m = Message(100, 1500, f"CQ {tag} VE3ABC FN03", -10)
            self.assertTrue(activation_cq(m))
            self.assertEqual(message_country(m), "Canada")
        self.assertFalse(activation_cq(Message(100, 1500, "CQ K4POTA EM85", -10)))
        self.assertEqual(
            message_country(Message(100, 1500, "K4TTL JA1ABC -10", -10)), "Japan"
        )
        self.assertEqual(
            message_country(Message(100, 1500, "JA1ABC K4TTL RRR", 0, True)), "Japan"
        )
