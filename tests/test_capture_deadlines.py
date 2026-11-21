import queue
import threading
import time
import unittest
from unittest.mock import patch
from ft8 import Decoder


class CaptureDeadlines(unittest.TestCase):
    def test_radio_feed_does_not_wait_for_decoder_lock(self):
        decoder = Decoder()
        decoder.activate()
        try:
            with decoder.lock:
                completed = threading.Event()
                worker = threading.Thread(
                    target=lambda: (decoder.feed(bytes(960)), completed.set())
                )
                worker.start()
                self.assertTrue(
                    completed.wait(0.1), "Network feed waited on decoder lock"
                )
            worker.join(1)
        finally:
            decoder.close()

    def test_overrun_discards_backlog_and_marks_gap_instead_of_joining_pcm(self):
        with patch.object(threading.Thread, "start"):
            decoder = Decoder()
        decoder.activate()
        for _ in range(60):
            decoder.feed(bytes(960))
        self.assertGreater(decoder.capture_dropped, 0)
        items = []
        while not decoder.capture_jobs.empty():
            items.append(decoder.capture_jobs.get_nowait())
        self.assertTrue(items[0][3])
        self.assertLessEqual(len(items), 48)

    def test_previous_band_pcm_cannot_enter_new_capture(self):
        with patch.object(threading.Thread, "start"):
            decoder = Decoder()
        decoder.activate()
        generation = decoder.generation
        decoder.activate(clear=True)
        decoder.capture(bytes(960), 1500.01, generation=generation)
        self.assertEqual(decoder.count, 0)
        self.assertIsNone(decoder.anchor)
