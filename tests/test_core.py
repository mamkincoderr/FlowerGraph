import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.ring_buffer import RingBuffer
from core.session import Session, Block, ChannelInfo
from core import file_io
from plugins.cobs_codec import cobs_decode, crc16


class RingBufferTests(unittest.TestCase):
    def test_get_span_does_not_need_the_whole_buffer(self):
        buf = RingBuffer(1, 64)
        t = np.arange(100, dtype=np.float64)
        v = t.reshape(-1, 1).astype(np.float32)
        buf.push(t, v)
        st, sv = buf.get_span(90, 95, max_points=None)
        self.assertIn(90.0, set(st.tolist()))
        self.assertIn(95.0, set(st.tolist()))
        self.assertGreaterEqual(float(st[0]), 89.0)
        self.assertLessEqual(float(st[-1]), 96.0)
        dec_t, dec_v = buf.get_span(0, 99, max_points=10)
        self.assertLessEqual(len(dec_t), 10)
        self.assertGreaterEqual(float(dec_t[0]), 36)
        self.assertLessEqual(float(dec_t[-1]), 99)
        self.assertEqual(len(dec_t), len(dec_v))

    def test_grow_keeps_wrapped_order_and_recent_lod_spans_window(self):
        buf = RingBuffer(1, 8)
        t = np.arange(8, dtype=np.float64)
        buf.push(t, t.reshape(-1, 1).astype(np.float32))
        buf.grow(20)
        buf.push(np.arange(8, 12, dtype=np.float64),
                 np.arange(8, 12, dtype=np.float32).reshape(-1, 1))
        got, _ = buf.get_last(12)
        np.testing.assert_array_equal(got, np.arange(12))
        # 100 Гц, окно 1 с должно покрыть последнюю секунду целиком
        sr = 100
        n = 250
        tt = np.arange(n, dtype=np.float64) / sr
        wave = np.sin(2 * np.pi * 2 * tt).astype(np.float32).reshape(-1, 1)
        big = RingBuffer(1, n)
        big.push(tt, wave)
        lt, lv = big.get_recent_lod(1.0, 80)
        self.assertGreaterEqual(float(lt[-1] - lt[0]), 0.7)
        self.assertLessEqual(len(lt), 80)
        self.assertGreater(float(np.max(lv)), 0.8)
        self.assertLess(float(np.min(lv)), -0.8)

    def test_zoom_out_stops_at_recorded_span(self):
        from ui.plot_area import TIME_DIV_SEQ, max_div_idx_for_span, N_DIV
        idx = max_div_idx_for_span(30.0)
        self.assertLessEqual(TIME_DIV_SEQ[idx] * N_DIV, 30.0 * 1.02)
        self.assertGreater(TIME_DIV_SEQ[idx + 1] * N_DIV, 30.0)

    def test_zoom_out_stops_at_recorded_span(self):
        from ui.plot_area import TIME_DIV_SEQ, max_div_idx_for_span, N_DIV
        idx = max_div_idx_for_span(30.0)
        self.assertLessEqual(TIME_DIV_SEQ[idx] * N_DIV, 30.0 * 1.02)
        self.assertGreater(TIME_DIV_SEQ[idx + 1] * N_DIV, 30.0)

    def test_lod_keeps_the_last_sample(self):
        from ui.plot_area import _lod_decimate
        n = 10_000
        t = np.linspace(0, 5, n)
        v = np.column_stack([np.sin(2 * np.pi * 3 * t), np.cos(t)]).astype(np.float32)
        td, vd = _lod_decimate(t, v, 8000)
        self.assertAlmostEqual(float(td[0]), float(t[0]), places=6)
        self.assertAlmostEqual(float(td[-1]), float(t[-1]), places=6)
        self.assertLessEqual(len(td), 8000)
        self.assertGreater(float(np.max(vd[:, 0])), 0.9)

    def test_push_get_last_wrap(self):
        buf = RingBuffer(2, 8)
        t = np.arange(10, dtype=np.float64)
        v = np.column_stack([t, t * 2]).astype(np.float32)
        buf.push(t, v)
        self.assertEqual(buf.size, 8)
        lt, lv = buf.get_last(3)
        np.testing.assert_array_equal(lt, t[-3:])
        np.testing.assert_array_almost_equal(lv[:, 0], t[-3:])


class FileIoTests(unittest.TestCase):
    def test_roundtrip(self):
        ses = Session()
        n = 20
        t = np.linspace(0, 1, n, dtype=np.float64)
        val = np.column_stack([np.sin(t), np.cos(t)]).astype(np.float32)
        ses.add_block(Block(
            start_time=1.0,
            source_name='gen',
            sample_rate=20,
            channels=[ChannelInfo('A', unit='V', scale=2.0, offset=0.1),
                      ChannelInfo('B')],
            times=t,
            values=val,
        ))
        fd, raw = tempfile.mkstemp(suffix='.fgd')
        os.close(fd)
        path = Path(raw)
        try:
            file_io.save(ses, path)
            loaded = file_io.load(path)
            self.assertEqual(loaded.n_blocks, 1)
            b = loaded.blocks[0]
            self.assertEqual(b.channels[0].name, 'A')
            self.assertEqual(b.channels[0].scale, 2.0)
            np.testing.assert_allclose(b.times, t)
            np.testing.assert_allclose(b.values, val)
        finally:
            path.unlink(missing_ok=True)
            Path(str(path) + '.tmp').unlink(missing_ok=True)


class CobsTests(unittest.TestCase):
    def test_decode_simple(self):
        # COBS of [0x11, 0x00, 0x22] without trailing 0 delimiter
        encoded = bytes([0x02, 0x11, 0x02, 0x22])
        self.assertEqual(cobs_decode(encoded), bytes([0x11, 0x00, 0x22]))

    def test_crc16_stable(self):
        self.assertEqual(crc16(b''), 0xFFFF)
        self.assertIsInstance(crc16(b'\x01\x02'), int)


class PgcScaleTests(unittest.TestCase):
    def test_peak_maps_to_int16(self):
        col = np.array([0.0, 0.5, -1.0], dtype=np.float64)
        peak = float(np.max(np.abs(col)))
        scale = peak / 32767.0
        raw = np.clip(np.round(col / scale), -32767, 32767).astype(np.int16)
        restored = raw.astype(np.float64) * scale
        np.testing.assert_allclose(restored, col, atol=1e-4)


if __name__ == '__main__':
    unittest.main()
