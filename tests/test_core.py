import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core.ring_buffer import RingBuffer
from core.session import Session, Block, ChannelInfo
from core import file_io
from plugins.com_cobs_source import _cobs_decode, _crc16


class RingBufferTests(unittest.TestCase):
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
        self.assertEqual(_cobs_decode(encoded), bytes([0x11, 0x00, 0x22]))

    def test_crc16_stable(self):
        self.assertEqual(_crc16(b''), 0xFFFF)
        self.assertIsInstance(_crc16(b'\x01\x02'), int)


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
