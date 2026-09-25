import unittest

import numpy as np

from core.measure import (
    fmt_span, fragment_stats, index_range, parse_y_text,
    scale_from_y_per_div, y_per_div_from_scale,
)


class MeasureTests(unittest.TestCase):
    def test_index_range_is_closed_open_on_edges(self):
        t = np.array([0.0, 0.1, 0.2, 0.3, 0.4])
        i0, i1 = index_range(t, 0.1, 0.3)
        self.assertEqual((i0, i1), (1, 4))
        self.assertEqual(list(t[i0:i1]), [0.1, 0.2, 0.3])

    def test_y_per_div_inverts_visual_scale(self):
        self.assertAlmostEqual(scale_from_y_per_div(0.1), 10.0)
        self.assertAlmostEqual(y_per_div_from_scale(10.0), 0.1)
        self.assertAlmostEqual(scale_from_y_per_div(1.0), 1.0)

    def test_parse_y_text_suffixes(self):
        self.assertAlmostEqual(parse_y_text('1,5'), 1.5)
        self.assertAlmostEqual(parse_y_text('10k'), 10000.0)
        self.assertAlmostEqual(parse_y_text('2m'), 0.002)
        self.assertAlmostEqual(parse_y_text('1e-3'), 1e-3)
        self.assertAlmostEqual(parse_y_text('1e-3m'), 1e-3)
        self.assertAlmostEqual(parse_y_text('1000 /дел'), 1000.0)

    def test_fragment_stats_std_is_not_rms(self):
        x = np.array([0.0, 0.0, 3.0, 3.0])
        st = fragment_stats(x)
        self.assertAlmostEqual(st['mean'], 1.5)
        self.assertAlmostEqual(st['rms'], np.sqrt(np.mean(x ** 2)))
        self.assertAlmostEqual(st['std'], float(np.std(x)))
        self.assertGreater(abs(st['std'] - st['rms']), 0.1)
        self.assertEqual(st['n'], 4)
        self.assertAlmostEqual(st['pp'], 3.0)

    def test_fmt_span(self):
        self.assertIn('мкс', fmt_span(12e-6))
        self.assertIn('мс', fmt_span(2.5e-3))
