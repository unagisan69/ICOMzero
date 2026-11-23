import itertools
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

from PIL import Image


class SpectrumZoom(unittest.TestCase):
    def setUp(self):
        self.module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'app/icomzero'))
        self.display = self.module['Display'].__new__(self.module['Display'])
        self.display.history = Image.new('RGB', (296, 70), '#080f20')
        self.display.sweep = self.display.edges = self.display.levels = None
        self.display.palette = [v for n in range(256) for v in (n, n, n)]
        self.display.zoom = 1
        bins = bytearray(512)
        bins[256] = 160
        self.scope = (1, 7000000, 7080000, bytes(bins))

    def test_zoom_keeps_centered_signal_and_original_widest_span(self):
        d = self.display
        for zoom in (1, 2, 4, 8, 4, 2, 1):
            d.zoom = zoom
            d.update_waterfall(self.scope, 7040000)
            self.assertEqual(d.edges, (7040000 - 40000 // zoom, 7040000 + 40000 // zoom))
            self.assertEqual(len(d.levels), 296)
            self.assertEqual(max(d.levels), 160)
            self.assertEqual(d.history.getpixel((148, 1)), (8, 15, 32))
        before = d.history.tobytes()
        d.update_waterfall(self.scope, 7040000)
        self.assertEqual(d.history.tobytes(), before)

    def test_zoom_clamps_to_available_scope_and_repaints_same_sweep_on_retune(self):
        d = self.display
        d.zoom = 8
        d.update_waterfall(self.scope, 7000000)
        self.assertEqual(d.edges, (7000000, 7010000))
        d.update_waterfall(self.scope, 7080000)
        self.assertEqual(d.edges, (7070000, 7080000))
        d.update_waterfall(None)
        self.assertIsNone(d.edges)

    def test_raw_fn_and_translated_arrows_zoom_with_limits_without_radio_commands(self):
        m = self.module
        with patch.dict(m['Receiver'].__init__.__globals__, {'read_config': lambda: {}}):
            rx = m['Receiver']()
        rx.frequency = 7040000
        rx.thread = Mock()
        rx.thread.is_alive.return_value = True
        rx.command = Mock()
        rx.tune = Mock()
        rx.radio_state = {'mode': 0}
        screen = Mock()
        # Raw Fn+F, then the stock driver's translated Fn+F/Fn+X keycodes.
        events = iter([(389, 1), (33, 1), (33, 0), (389, 0),
                       (103, 1), (103, 1), (103, 1),
                       (108, 1), (108, 1), (108, 1), (108, 1), (1, 1)])
        clock = itertools.count(10, .1)
        with patch.dict(m['ui'].__globals__, {
            'Display': lambda _: screen, 'Receiver': lambda: rx,
            'load': lambda: ['192.0.2.1', '50001', 'test', 'test'],
            'keys_ready': lambda *_: [next(events)],
        }), patch('time.monotonic', side_effect=lambda: next(clock)), patch('cw.Decoder'):
            m['ui'](-1, -1)
        zooms = [call.args[-1] for call in screen.render.call_args_list]
        self.assertEqual(zooms, [1, 1, 2, 2, 2, 4, 8, 8, 4, 2, 1, 1])
        rx.command.assert_not_called()
        rx.tune.assert_not_called()
