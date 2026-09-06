"""
FG-NET: разбор самоописывающейся схемы и пакета данных.

Слой FgNetSchema / parse_schema_payload / parse_packet не зависит от Qt —
эти тесты идут в CInском окружении, где стоит только numpy (см.
build-and-release.yml, job `test`). PySide6 здесь не импортируется.
"""

import gzip
import json
import struct
import unittest
import zlib

import numpy as np

from plugins.fg_net_source import (
    FGNET_FMT_STRUCTURED_V1,
    FGNET_TYPE_DATA,
    FgNetConfig,
    FgNetSchema,
    _DEFAULT_SCHEMA,
    _HDR_FMT,
    _MAGIC,
    parse_announce,
    parse_packet,
    parse_schema_payload,
)

# Компактная искусственная схема: 2 поля устройства, 3 поля системы,
# scope 4×2. Проверяет, что раскладка байт считается из схемы, а не зашита.
_MINI_SCHEMA = {
    "schema": 1,
    "profile": "MINI_TEST",
    "blocks": [
        {"id": "dev", "repeat": 2, "label": "Плата %d", "fields": [
            {"k": "V", "t": "f", "l": "Напряжение", "u": "В", "d": 1},
            {"k": "st", "t": "B", "l": "Состояние"},
        ]},
        {"id": "sys", "label": "Общее", "fields": [
            {"k": "U", "t": "f", "l": "U шины", "u": "В", "d": 1},
            {"k": "cnt", "t": "I", "l": "Счётчик"},
            {"k": "flag", "t": "B", "l": "Флаг"},
        ]},
        {"id": "scope", "n": 4, "ch": 2, "scale": 0.5, "label": "Осц",
         "channels": [{"l": "A", "u": "В"}, {"l": "B", "u": "А"}]},
    ],
}


class SchemaLayoutTests(unittest.TestCase):
    def setUp(self):
        self.sc = FgNetSchema(_MINI_SCHEMA)

    def test_sizes_from_fields(self):
        self.assertEqual(self.sc.dev_size, 4 + 1)          # f + B
        self.assertEqual(self.sc.sys_size, 4 + 4 + 1)      # f + I + B
        self.assertEqual(self.sc.scope_size, 1 + 4 * 2 * 2)
        self.assertEqual(self.sc.off_sys, 2 * 5)
        self.assertEqual(self.sc.off_scope, 10 + 9)
        self.assertEqual(self.sc.body_size, 19 + 17)
        self.assertEqual(self.sc.pkt_size, 28 + self.sc.body_size + 4)

    def test_size_mismatch_rejected(self):
        bad = json.loads(json.dumps(_MINI_SCHEMA))
        bad["blocks"][0]["size"] = 99
        with self.assertRaises(ValueError):
            FgNetSchema(bad)

    def test_groups_and_labels(self):
        titles = [t for t, _ in self.sc.groups()]
        self.assertEqual(titles, ["Общее", "Плата 1", "Плата 2", "Осц"])
        self.assertEqual(self.sc.label("sys.U"), "U шины")
        self.assertEqual(self.sc.label("dev2.V"), "М2 Напряжение")
        self.assertEqual(self.sc.label("scope.1"), "A")

    def test_default_channels_from_flag(self):
        self.assertEqual(self.sc.default_channels(), ["sys.U", "dev1.V"])

    def test_parse_body_roundtrip(self):
        body = bytearray(self.sc.body_size)
        struct.pack_into("<fB", body, 0, 12.5, 3)           # dev0
        struct.pack_into("<fB", body, 5, 400.0, 1)          # dev1
        struct.pack_into("<fIB", body, self.sc.off_sys, 230.0, 777, 1)
        struct.pack_into("<8h", body, self.sc.off_scope + 1,
                         1, 2, 3, 4, 5, 6, 7, 8)
        dev, sysd, scope = self.sc.parse_body(bytes(body))
        self.assertAlmostEqual(dev[0]["V"], 12.5)
        self.assertEqual(dev[0]["st"], 3)
        self.assertAlmostEqual(dev[1]["V"], 400.0)
        self.assertAlmostEqual(sysd["U"], 230.0)
        self.assertEqual(sysd["cnt"], 777)
        self.assertEqual(list(scope), [1, 2, 3, 4, 5, 6, 7, 8])

    def test_resolve(self):
        dev = [{"V": 1.0, "st": 0}, {"V": 2.0, "st": 0}]
        sysd = {"U": 230.0, "cnt": 0, "flag": 0}
        scope = np.arange(8, dtype=np.int16)
        self.assertEqual(self.sc.resolve("sys.U", dev, sysd, scope), 230.0)
        self.assertEqual(self.sc.resolve("dev2.V", dev, sysd, scope), 2.0)
        np.testing.assert_array_equal(
            self.sc.resolve("scope.2", dev, sysd, scope), np.array([4, 5, 6, 7]))
        self.assertEqual(self.sc.resolve("bogus.x", dev, sysd, scope), 0.0)


class DefaultSchemaTests(unittest.TestCase):
    def test_default_matches_firmware_contract(self):
        sc = FgNetSchema.default()
        self.assertEqual(sc.dev_size, 80)
        self.assertEqual(sc.sys_size, 88)
        self.assertEqual(sc.body_size, 935)
        self.assertEqual(sc.pkt_size, 967)
        self.assertEqual(sc.dev_count, 6)
        self.assertEqual(sc.scope_n, 61)
        self.assertEqual(sc.default_channels(), ["sys.U_in", "sys.U_out", "sys.I_out"])

    def test_default_schema_dict_is_json_safe(self):
        json.dumps(_DEFAULT_SCHEMA)


class SchemaPayloadTests(unittest.TestCase):
    def test_plain_json(self):
        raw = json.dumps(_MINI_SCHEMA).encode()
        self.assertEqual(parse_schema_payload(raw)["profile"], "MINI_TEST")

    def test_gzipped(self):
        raw = gzip.compress(json.dumps(_MINI_SCHEMA).encode())
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(parse_schema_payload(raw)["profile"], "MINI_TEST")

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_schema_payload(b"\x00\x01\x02not json"))
        self.assertIsNone(parse_schema_payload(b""))


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.sc = FgNetSchema.default()

    def _packet(self, body: bytes, *, bad_crc=False, seq=1):
        hdr = struct.pack(_HDR_FMT, _MAGIC, 1, FGNET_TYPE_DATA, 0,
                          0xAABBCCDD, seq, 123456, 0,
                          FGNET_FMT_STRUCTURED_V1, 0, self.sc.body_size)
        crc = zlib.crc32(hdr + body) ^ (0xFFFFFFFF if bad_crc else 0)
        return hdr + body + struct.pack("<I", crc & 0xFFFFFFFF)

    def test_roundtrip(self):
        body = bytearray(self.sc.body_size)
        struct.pack_into("<f", body, self.sc.off_sys, 231.7)       # sys.U_in
        struct.pack_into("<f", body, 0, 48.9)                      # dev0.Uo_RMS
        raw = self._packet(bytes(body))
        self.assertEqual(len(raw), self.sc.pkt_size)
        res = parse_packet(raw, self.sc)
        self.assertIsNotNone(res)
        hdr, dev, sysd, scope = res
        self.assertEqual(hdr.sequence, 1)
        self.assertAlmostEqual(sysd["U_in"], 231.7, places=3)
        self.assertAlmostEqual(dev[0]["Uo_RMS"], 48.9, places=3)
        self.assertEqual(len(scope), self.sc.scope_n * self.sc.scope_ch)

    def test_bad_crc_rejected(self):
        raw = self._packet(bytes(self.sc.body_size), bad_crc=True)
        self.assertIsNone(parse_packet(raw, self.sc))

    def test_wrong_length_rejected(self):
        raw = self._packet(bytes(self.sc.body_size))
        self.assertIsNone(parse_packet(raw[:-1], self.sc))


class ConfigTests(unittest.TestCase):
    def test_roundtrip_keeps_known_fields(self):
        c = FgNetConfig(device_ip="10.0.0.5", telemetry_decim=6,
                        channels=["scope.1", "sys.U_out"])
        c2 = FgNetConfig.from_dict(c.to_dict())
        self.assertEqual(c2.device_ip, "10.0.0.5")
        self.assertEqual(c2.telemetry_decim, 6)
        self.assertEqual(c2.channels, ["scope.1", "sys.U_out"])

    def test_has_scope(self):
        self.assertTrue(FgNetConfig(channels=["scope.1"]).has_scope())
        self.assertTrue(FgNetConfig(channels=["sys.U_out", "scope.2"]).has_scope())
        self.assertFalse(FgNetConfig(channels=["sys.U_in"]).has_scope())


class AnnounceTests(unittest.TestCase):
    @staticmethod
    def _announce(name=b"BENCH-01", schema_crc=None):
        # fgnet_announce_t (packed): B B B I B  char[32]  [+ I]
        p = struct.pack("<BBBIB", 1, 4, 3, 2000, 1)          # 8 байт, offset name = 8
        p += name.ljust(32, b"\x00")
        if schema_crc is not None:
            p += struct.pack("<I", schema_crc)
        return p

    def test_legacy_40_bytes(self):
        d = parse_announce(self._announce())
        self.assertEqual(d["fw"], "1.4")
        self.assertEqual(d["max_sample_rate"], 2000)
        self.assertEqual(d["name"], "BENCH-01")
        self.assertNotIn("schema_crc", d)

    def test_44_bytes_with_schema_crc(self):
        d = parse_announce(self._announce(schema_crc=0x0720ABCD))
        self.assertEqual(d["name"], "BENCH-01")
        self.assertEqual(d["schema_crc"], 0x0720ABCD)


if __name__ == "__main__":
    unittest.main()
