"""
FG-NET источник данных — приём телеметрии по Wi-Fi UDP от встраиваемого
устройства.

Профиль STRUCTURED_V1: пакет = заголовок(28) + тело(935) + CRC32(4) = 967 байт.
Тело — фиксированный снимок: 6 записей устройств + общий системный блок +
один буфер осциллографа (3 канала × 61 точка).

Три независимых сетевых канала:
  UDP  5000 — поток данных
  TCP  5001 — управление (PING/STATUS/START/STOP, heartbeat раз в 1 с)
  UDP  5002 — discovery (multicast 239.10.10.1)

Осциллограф уходит в график через _emit(times, values) как временной ряд
(61×3). Скалярная телеметрия — не через _emit, а в свойство .telemetry
(dict), обновляется на каждый пакет (отображение — отдельная задача).
"""

import gzip
import json
import queue
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

import numpy as np

from plugins.base_source import BaseSource, put_drop_oldest

# PySide6 импортируется лениво (перед классом диалога, ниже): слой разбора
# пакета и схемы — FgNetSchema, parse_packet, parse_schema_payload,
# fetch_schema — от Qt не зависит и обязан импортироваться в headless-окружении
# (CI-джоб `test` ставит только numpy). См. tests/test_fg_net.py.


# ---------------------------------------------------------------------------
# Транспорт — фиксирован (fg_net_wire.h). Раскладку ТЕЛА пакета описывает
# схема, которую устройство отдаёт по GET_SCHEMA (см. FgNetSchema ниже).
# ---------------------------------------------------------------------------

_HDR_FMT   = '<4sBBBIIQBBBH'
_HDR_SIZE  = struct.calcsize(_HDR_FMT)          # 28
_MAGIC     = b'FGNT'

FGNET_VERSION = 0x01
FGNET_TYPE_DATA               = 0x01
FGNET_TYPE_DISCOVERY_ANNOUNCE = 0x02
FGNET_TYPE_DISCOVERY_REQUEST  = 0x03
FGNET_FMT_STRUCTURED_V1       = 0x05

FGNET_FLAG_OVERFLOW       = 1 << 0
FGNET_FLAG_CLOCK_UNSYNCED = 1 << 1

_CMD_PING, _CMD_PONG          = 0x01, 0x02
_CMD_GET_STATUS, _CMD_STATUS  = 0x10, 0x11
_CMD_GET_SCHEMA, _CMD_SCHEMA  = 0x12, 0x13
_CMD_START, _CMD_STOP         = 0x20, 0x21
_CMD_ACK, _CMD_ERROR          = 0x22, 0x30

# struct-код по типу поля из схемы
_T_STRUCT = {'f': 'f', 'I': 'I', 'i': 'i', 'H': 'H', 'h': 'h', 'B': 'B', 'b': 'b'}
_T_SIZE   = {'f': 4, 'I': 4, 'i': 4, 'H': 2, 'h': 2, 'B': 1, 'b': 1}


# ---------------------------------------------------------------------------
# Схема тела пакета — самоописание от устройства (GET_SCHEMA -> JSON).
# Разбирает: раскладку байт (из порядка+типов полей), подписи, единицы,
# каналы по умолчанию, параметры осциллографа. Если устройство схему не
# отдало — используется _DEFAULT_SCHEMA (generic-подписи).
# ---------------------------------------------------------------------------

# Встроенная схема-заглушка: раскладка STRUCTURED_V1, обезличенные подписи.
_DEFAULT_SCHEMA = {
    "schema": 1, "profile": "STRUCTURED_V1",
    "packet": {"hdr": 28, "body": 935, "crc": 4},
    "blocks": [
        {"id": "dev", "repeat": 6, "size": 80, "label": "Устройство %d", "fields": [
            {"k": k, "t": t} for k, t in [
                ("Uo_RMS", "f"), ("Uin_RMS", "f"), ("Iin_RMS", "f"), ("Iout_RMS", "f"),
                ("Udc", "f"), ("Ia_RMS", "f"), ("Fgrid", "f"), ("T_module", "f"),
                ("T_dross", "f"), ("HalfRef", "f"), ("V15v", "f"),
                ("CPU_Load", "I"), ("MAX_CPU_Load", "I"), ("SoftVersion", "I"),
                ("error_code", "I"), ("UID1", "I"), ("UID2", "I"),
                ("ENA", "B"), ("Mode1", "B"), ("Mode2", "B"), ("Cmd", "B"), ("Index", "B"),
                ("Ref1", "h"), ("Ref2", "h"), ("DataTor", "h"), ("state", "B"),
            ]]},
        {"id": "sys", "size": 88, "label": "Система", "fields": [
            {"k": k, "t": t, **({"d": 1} if k in ("U_in", "U_out", "I_out") else {})}
            for k, t in [
                ("U_in", "f"), ("U_out", "f"), ("I_in", "f"), ("I_out", "f"),
                ("W_Full", "f"), ("P_Activ", "f"), ("Q_Reactiv", "f"), ("P_nom", "f"),
                ("F_sr", "f"), ("State_CountDownn", "I"), ("ErrCode", "I"),
                ("Soft_V_master", "I"), ("Soft_V_slave", "I"), ("U_Supp", "f"),
                ("LifeTime", "I"), ("SesionTime", "I"), ("Fan_rev_L", "I"), ("Fan_rev_H", "I"),
                ("Trad", "f"), ("Tdr", "f"), ("Q_fan", "f"), ("N_fan", "f"),
            ]]},
        {"id": "scope", "n": 61, "ch": 3, "scale": 0.1, "label": "Осциллограф",
         "channels": [{"l": "CH1"}, {"l": "CH2"}, {"l": "CH3"}]},
    ],
}


class FgNetSchema:
    """Разобранная схема тела пакета."""

    def __init__(self, d: dict):
        self.raw = d
        self.version = d.get("schema", 1)
        self.profile = d.get("profile", "STRUCTURED_V1")
        blk = {b["id"]: b for b in d["blocks"]}
        dev, sys_, sc = blk["dev"], blk["sys"], blk["scope"]

        self.dev_count = int(dev.get("repeat", 6))
        self.dev_keys  = [f["k"] for f in dev["fields"]]
        self.dev_fmt   = "<" + "".join(_T_STRUCT[f["t"]] for f in dev["fields"])
        self.dev_size  = struct.calcsize(self.dev_fmt)
        self.sys_keys  = [f["k"] for f in sys_["fields"]]
        self.sys_fmt   = "<" + "".join(_T_STRUCT[f["t"]] for f in sys_["fields"])
        self.sys_size  = struct.calcsize(self.sys_fmt)
        self.scope_n     = int(sc["n"])
        self.scope_ch    = int(sc.get("ch", 3))
        self.scope_scale = float(sc.get("scale", 0.1))
        self.scope_size  = 1 + self.scope_n * self.scope_ch * 2

        self.off_sys   = self.dev_count * self.dev_size
        self.off_scope = self.off_sys + self.sys_size
        self.body_size = self.off_scope + self.scope_size
        self.pkt_size  = _HDR_SIZE + self.body_size + 4

        # sanity: declared vs computed
        for b, sz in ((dev, self.dev_size), (sys_, self.sys_size)):
            if "size" in b and b["size"] != sz:
                raise ValueError(f'schema block {b["id"]}: size {b["size"]} != {sz}')
        pk = d.get("packet", {})
        if pk.get("body") and pk["body"] != self.body_size:
            raise ValueError(f'schema body {pk["body"]} != {self.body_size}')

        # подписи / единицы / дефолт
        self._dev_lbl  = {f["k"]: f.get("l", f["k"]) for f in dev["fields"]}
        self._sys_lbl  = {f["k"]: f.get("l", f["k"]) for f in sys_["fields"]}
        self._dev_unit = {f["k"]: f.get("u", "") for f in dev["fields"]}
        self._sys_unit = {f["k"]: f.get("u", "") for f in sys_["fields"]}
        self._sc_lbl   = [c.get("l", f"CH{i+1}") for i, c in enumerate(sc.get("channels", []))]
        self._sc_unit  = [c.get("u", "") for c in sc.get("channels", [])]
        self.dev_group   = dev.get("label", "Устройство %d")
        self.sys_group   = sys_.get("label", "Система")
        self.scope_group = sc.get("label", "Осциллограф")
        self._dev_def = {f["k"] for f in dev["fields"] if f.get("d")}
        self._sys_def = {f["k"] for f in sys_["fields"] if f.get("d")}

    # -- каталог сигналов для дерева -----------------------------------

    def groups(self):
        """[(заголовок, [(key, label), ...]), ...] для дерева выбора."""
        out = [(self.sys_group,
                [(f"sys.{k}", self._sys_lbl[k]) for k in self.sys_keys])]
        for n in range(1, self.dev_count + 1):
            title = self.dev_group % n if "%" in self.dev_group else f"{self.dev_group} {n}"
            out.append((title, [(f"dev{n}.{k}", self._dev_lbl[k]) for k in self.dev_keys]))
        out.append((self.scope_group,
                    [(f"scope.{i+1}", self._sc_lbl[i] if i < len(self._sc_lbl) else f"Scope {i+1}")
                     for i in range(self.scope_ch)]))
        return out

    def default_channels(self):
        keys = [f"sys.{k}" for k in self.sys_keys if k in self._sys_def]
        keys += [f"dev1.{k}" for k in self.dev_keys if k in self._dev_def]
        return keys or ([f"sys.{self.sys_keys[0]}"] if self.sys_keys else [])

    def label(self, key: str) -> str:
        if key.startswith("sys."):
            return self._sys_lbl.get(key[4:], key[4:])
        if key.startswith("scope."):
            i = int(key.split(".", 1)[1]) - 1
            return self._sc_lbl[i] if 0 <= i < len(self._sc_lbl) else key
        if key.startswith("dev") and "." in key:
            n, k = key[3:].split(".", 1)
            return f"М{n} " + self._dev_lbl.get(k, k)
        return key

    # -- разбор одного пакета -----------------------------------------

    def to_dict(self):
        return self.raw

    def parse_body(self, body: bytes):
        devices = [dict(zip(self.dev_keys, struct.unpack_from(self.dev_fmt, body, i * self.dev_size)))
                   for i in range(self.dev_count)]
        system = dict(zip(self.sys_keys, struct.unpack_from(self.sys_fmt, body, self.off_sys)))
        n = self.scope_n * self.scope_ch
        scope = np.asarray(struct.unpack_from(f"<{n}h", body, self.off_scope + 1), dtype=np.int16)
        return devices, system, scope

    def resolve(self, key: str, devices: list, system: dict, scope: np.ndarray):
        """Значение канала: скаляр или np.ndarray(scope_n,)."""
        if key.startswith("sys."):
            return float(system.get(key[4:], 0.0))
        if key.startswith("scope."):
            c = int(key.split(".", 1)[1]) - 1
            if 0 <= c < self.scope_ch:
                return scope[c * self.scope_n:(c + 1) * self.scope_n].astype(np.float32)
            return np.zeros(self.scope_n, dtype=np.float32)
        if key.startswith("dev") and "." in key:
            n, k = key[3:].split(".", 1)
            i = int(n) - 1
            if 0 <= i < len(devices):
                return float(devices[i].get(k, 0.0))
        return 0.0

    @classmethod
    def default(cls):
        return cls(_DEFAULT_SCHEMA)


FGNET_DEFAULT_CHANNELS = FgNetSchema.default().default_channels()  # sys.U_in/U_out/I_out


def parse_schema_payload(payload: bytes) -> dict | None:
    """Payload ответа SCHEMA -> dict. gzip (магия 1f 8b) распаковывается."""
    try:
        raw = gzip.decompress(payload) if payload[:2] == b"\x1f\x8b" else payload
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def fetch_schema(ip: str, control_port: int = 5001, timeout: float = 2.5) -> dict | None:
    """Короткий TCP-connect к устройству, GET_SCHEMA -> dict или None."""
    try:
        s = socket.create_connection((ip, control_port), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(struct.pack("<BH", _CMD_GET_SCHEMA, 0))
        h = b""
        while len(h) < 3:
            c = s.recv(3 - len(h))
            if not c:
                s.close(); return None
            h += c
        ln = h[1] | (h[2] << 8)
        p = b""
        while len(p) < ln:
            c = s.recv(min(4096, ln - len(p)))
            if not c:
                break
            p += c
        s.close()
        if h[0] != _CMD_SCHEMA or len(p) != ln:
            return None
        return parse_schema_payload(p)
    except OSError:
        return None


class FgNetHeader:
    __slots__ = ('version', 'type', 'flags', 'device_id', 'sequence',
                 'timestamp_us', 'sample_format', 'payload_length')

    def __init__(self, tup):
        (_magic, self.version, self.type, self.flags, self.device_id,
         self.sequence, self.timestamp_us, _nch, self.sample_format,
         _nsamp, self.payload_length) = tup


def parse_header(raw: bytes):
    if len(raw) < _HDR_SIZE:
        return None
    tup = struct.unpack_from(_HDR_FMT, raw, 0)
    if tup[0] != _MAGIC:
        return None
    return FgNetHeader(tup)


def parse_announce(payload: bytes) -> dict:
    # fgnet_announce_t: BB B I B  char[32]  [+ I schema_crc]  (40 или 44 Б)
    if len(payload) < 40:
        return {}
    maj, minr, mch, msr, state = struct.unpack_from('<BBBIB', payload, 0)
    name = payload[8:40].split(b'\x00', 1)[0].decode('utf-8', 'replace')
    d = dict(fw=f'{maj}.{minr}', max_channels=mch, max_sample_rate=msr,
             state=state, name=name)
    if len(payload) >= 44:
        d['schema_crc'] = struct.unpack_from('<I', payload, 40)[0]
    return d


def parse_packet(raw: bytes, schema: FgNetSchema):
    """-> (FgNetHeader, list[dict] devices, dict system, np.int16 scope) | None"""
    if len(raw) != schema.pkt_size:
        return None
    hdr = parse_header(raw)
    if hdr is None or hdr.type != FGNET_TYPE_DATA \
            or hdr.sample_format != FGNET_FMT_STRUCTURED_V1:
        return None

    end = _HDR_SIZE + schema.body_size
    crc_rx = struct.unpack_from('<I', raw, end)[0]
    if zlib.crc32(raw[:end]) != crc_rx:
        return None

    devices, system, scope = schema.parse_body(raw[_HDR_SIZE:end])
    return hdr, devices, system, scope


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class FgNetConfig:
    device_id:       int | None = None
    device_ip:       str = ''
    control_port:    int = 5001
    data_port:       int = 5000
    discovery_port:  int = 5002
    discovery_group: str = '239.10.10.1'
    # частота дискретизации осциллографа на стороне устройства (2000 Гц —
    # 61 точка = окно ~30.5 мс). Приходит в discovery-анонсе (max_sample_rate).
    scope_rate:      int = 2000
    # int16 -> физические единицы. Устройство нормализует все каналы к *10,
    # обратно /10 = 0.1 (как в его штатном осциллографе).
    scope_scale:     float = 0.1
    # Делитель частоты телеметрии по служебному кадру внутреннего опроса
    # устройства (~400 Гц). Пакет на каждый N-й -> частота ≈ 400/N Гц. 0 = как 1.
    # Осциллограмма повторяется между захватами (+ независимо ~10-14 Гц по
    # завершению захвата). ВНИМАНИЕ: высокая частота нагружает HTTP устройства.
    telemetry_decim: int = 4
    # какие сигналы писать/строить. Ключи — sys.<k> / dev<N>.<k> / scope.<c>.
    channels:        list[str] = field(default_factory=lambda: list(FGNET_DEFAULT_CHANNELS))
    # последняя полученная от устройства схема тела пакета (GET_SCHEMA) + её CRC.
    # Хранится, чтобы диалог/источник работали, если устройство недоступно.
    schema:          dict | None = None
    schema_crc:      int = 0

    def selected(self) -> list[str]:
        return list(self.channels) if self.channels else list(FGNET_DEFAULT_CHANNELS)

    def has_scope(self) -> bool:
        return any(k.startswith('scope.') for k in self.selected())

    def make_schema(self) -> 'FgNetSchema':
        if self.schema:
            try:
                return FgNetSchema(self.schema)
            except Exception:
                pass
        return FgNetSchema.default()

    def to_dict(self) -> dict:
        return {
            'device_id': self.device_id, 'device_ip': self.device_ip,
            'control_port': self.control_port, 'data_port': self.data_port,
            'discovery_port': self.discovery_port,
            'discovery_group': self.discovery_group,
            'scope_rate': self.scope_rate, 'scope_scale': self.scope_scale,
            'telemetry_decim': self.telemetry_decim,
            'channels': list(self.channels),
            'schema': self.schema, 'schema_crc': self.schema_crc,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'FgNetConfig':
        f = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in f})


# ---------------------------------------------------------------------------
# Discovery (multicast :5002)
# ---------------------------------------------------------------------------

class FgNetDiscovery:
    def __init__(self, group='239.10.10.1', port=5002):
        self._group = group
        self._port = port
        self._sock: socket.socket | None = None
        self._devices: dict[int, dict] = {}
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        if self._running:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('', self._port))
            mreq = struct.pack('4sl', socket.inet_aton(self._group), socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.settimeout(0.5)
        except OSError:
            return
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def send_request(self):
        if not self._sock:
            return
        pkt = struct.pack(_HDR_FMT, _MAGIC, FGNET_VERSION,
                          FGNET_TYPE_DISCOVERY_REQUEST, 0, 0, 0, 0, 0, 0, 0, 0)
        try:
            self._sock.sendto(pkt, (self._group, self._port))
        except OSError:
            pass

    def _loop(self):
        while self._running:
            try:
                raw, addr = self._sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            hdr = parse_header(raw)
            if hdr is None or hdr.type != FGNET_TYPE_DISCOVERY_ANNOUNCE:
                continue
            info = parse_announce(raw[_HDR_SIZE:])
            info['ip'] = addr[0]
            info['device_id'] = hdr.device_id
            info['seen'] = time.monotonic()
            self._devices[hdr.device_id] = info

    @property
    def devices(self) -> dict[int, dict]:
        now = time.monotonic()
        return {k: v for k, v in self._devices.items() if now - v.get('seen', 0) < 8.0}


# ---------------------------------------------------------------------------
# Control-клиент (TCP :5001)
# ---------------------------------------------------------------------------

class FgNetControl:
    def __init__(self, ip: str, port: int = 5001):
        self._sock = socket.create_connection((ip, port), timeout=3.0)
        self._sock.settimeout(1.0)
        self._running = False
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self.last_status: dict | None = None
        self.link_ok = True

    # -- публичное ----------------------------------------------------------

    def start_stream(self, dest_port: int, scope_rate: int = 2000, decim: int = 0):
        # payload '<IHBIB': ip(u32,LE)=0 -> ESP32 берёт IP TCP-пира; port(u16,LE);
        # channel_mask(u8, игнор); scope_rate(u32, игнор устройством); decim(u8).
        payload = struct.pack('<IHBIB', 0, dest_port, 0xFF, scope_rate,
                              max(0, min(255, int(decim))))
        self._send(_CMD_START, payload)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop_stream(self):
        self._running = False
        try:
            self._send(_CMD_STOP, b'')
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self._sock.close()
        except OSError:
            pass

    def request_status(self):
        try:
            self._send(_CMD_GET_STATUS, b'')
        except OSError:
            pass

    # -- внутреннее -------------------------------------------------------

    def _send(self, cmd: int, payload: bytes):
        frame = struct.pack('<BH', cmd, len(payload)) + payload
        with self._send_lock:
            self._sock.sendall(frame)

    def _loop(self):
        buf = bytearray()
        last_ping = 0.0
        last_status = 0.0
        while self._running:
            now = time.monotonic()
            if now - last_ping >= 1.0:
                last_ping = now
                try:
                    self._send(_CMD_PING, b'')
                    if now - last_status >= 5.0:   # периодический STATUS для диагностики/соака
                        last_status = now
                        self._send(_CMD_GET_STATUS, b'')
                except OSError:
                    self.link_ok = False
                    break
            try:
                chunk = self._sock.recv(512)
                if not chunk:
                    self.link_ok = False
                    break
                buf.extend(chunk)
            except socket.timeout:
                continue
            except OSError:
                self.link_ok = False
                break

            while len(buf) >= 3:
                cmd = buf[0]
                ln = buf[1] | (buf[2] << 8)
                if len(buf) < 3 + ln:
                    break
                payload = bytes(buf[3:3 + ln])
                del buf[:3 + ln]
                if cmd == _CMD_STATUS and ln >= 13:
                    up, ovf, heap, streaming = struct.unpack_from('<IIIB', payload, 0)
                    self.last_status = dict(uptime=up, overflow=ovf,
                                            free_heap=heap, streaming=streaming)


# ---------------------------------------------------------------------------
# Источник данных
# ---------------------------------------------------------------------------

class FgNetSource(BaseSource):
    _DRAIN_MS = 15

    def __init__(self, config: FgNetConfig | None = None):
        super().__init__()
        self._config = config or FgNetConfig()
        self._queue: queue.Queue = queue.Queue(maxsize=512)
        self._control: FgNetControl | None = None
        self._thread: threading.Thread | None = None

        self._pkt_ok = 0
        self._pkt_err = 0
        self._pkt_lost = 0
        self._pkt_overflow = 0     # флаг OVERFLOW в заголовке — потери на плате
        self._pkt_dup_scope = 0    # пакеты с повторным буфером scope (при прореживании)
        self._last_seq = -1
        self._last_scope_key = None
        self._t_offset_us = 0
        self._sel = self._config.selected()   # зафиксировать на время сессии
        self._n_ch = len(self._sel)

        self._telemetry: dict = {}
        self._schema = self._config.make_schema()

        from PySide6.QtCore import QTimer
        self._drain_timer = QTimer()
        self._drain_timer.timeout.connect(self._drain_queue)

    # -- BaseSource -------------------------------------------------------

    def get_name(self) -> str:
        ip = self._config.device_ip or '?'
        return f'FG-NET ({ip}:{self._config.data_port})'

    def get_channel_count(self) -> int:
        return self._n_ch or len(FGNET_DEFAULT_CHANNELS)

    def get_channel_names(self) -> list[str]:
        return [self._schema.label(k) for k in self._sel]

    def get_config_widget(self):
        return FgNetConfigDialog(self._config)

    def effective_sample_rate(self) -> int:
        # scope выбран -> частота дискретизации осциллографа (~2000 Гц); иначе —
        # темп пакетов: по scope ~12 Гц, либо ~400/decim при прореживании.
        if self._config.has_scope() and self._config.scope_rate > 0:
            return int(self._config.scope_rate)
        d = max(1, self._config.telemetry_decim)
        return 400 // d

    def start(self) -> bool:
        if not self._config.device_ip:
            self._emit_error('FG-NET: устройство не выбрано')
            self._drain_errors()
            return False

        # свежая схема тела пакета от устройства (иначе — из конфига / дефолт)
        d = fetch_schema(self._config.device_ip, self._config.control_port)
        if d:
            try:
                self._schema = FgNetSchema(d)
                self._config.schema = d
                self._config.schema_crc = zlib.crc32(json.dumps(d).encode())
            except Exception as e:
                self._emit_error(f'FG-NET: схема устройства не разобрана ({e}), беру прежнюю')
        self._sel = self._config.selected()
        self._n_ch = len(self._sel)

        try:
            self._control = FgNetControl(self._config.device_ip,
                                         self._config.control_port)
            self._control.start_stream(self._config.data_port,
                                       self._config.scope_rate,
                                       self._config.telemetry_decim)
        except OSError as e:
            self._emit_error(f'FG-NET: не удалось подключиться к '
                             f'{self._config.device_ip}:{self._config.control_port} — {e}')
            self._drain_errors()
            self._control = None
            return False

        self._pkt_ok = self._pkt_err = self._pkt_lost = self._pkt_overflow = 0
        self._pkt_dup_scope = 0
        self._last_seq = -1
        self._last_scope_key = None
        self._t_offset_us = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._drain_timer.start(self._DRAIN_MS)
        return True

    def stop(self):
        self._running = False
        self._drain_timer.stop()
        if self._control:
            try:
                self._control.stop_stream()
            except OSError:
                pass
            self._control = None
        if self._thread:
            self._thread.join(timeout=0.6)
            self._thread = None
        self._drain_queue()

    # -- приём ------------------------------------------------------------

    def _read_loop(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', self._config.data_port))
            sock.settimeout(0.2)
        except OSError as e:
            self._emit_error(f'FG-NET: не удалось открыть UDP:{self._config.data_port} — {e}')
            self._running = False
            return

        sch         = self._schema
        has_scope   = self._config.has_scope()
        scope_scale = self._config.scope_scale
        dt          = 1.0 / max(1, self._config.scope_rate)
        idx61       = np.arange(sch.scope_n, dtype=np.float64)

        while self._running:
            try:
                raw, _ = sock.recvfrom(2048)
            except socket.timeout:
                # заодно ловим обрыв управления
                if self._control and not self._control.link_ok:
                    self._emit_error('FG-NET: связь управления потеряна')
                    break
                continue
            except OSError as e:
                self._emit_error(f'FG-NET: приём UDP — {e}')
                continue

            res = parse_packet(raw, sch)
            if res is None:
                self._pkt_err += 1
                continue

            hdr, devices, system, scope = res
            self._check_sequence(hdr.sequence)
            if hdr.flags & FGNET_FLAG_OVERFLOW:
                self._pkt_overflow += 1

            # Телеметрия свежая в каждом пакете — обновляем свойство всегда.
            self._telemetry = {
                'ts_us': hdr.timestamp_us,
                'seq': hdr.sequence,
                'devices': devices,
                'system': system,
            }
            self._pkt_ok += 1

            # В режиме осциллографа: буфер захвата обновляется у устройства
            # только ~10-14 Гц. При прореживании (decim) пакеты идут чаще, и один
            # и тот же захват повторяется. Класть его в график повторно нельзя —
            # окна 30.5 мс перекрываются, время идёт назад, pyqtgraph рисует мусор.
            # Пропускаем пакет, если scope-блок не изменился.
            if has_scope:
                scope_key = scope.tobytes()
                if scope_key == self._last_scope_key:
                    self._pkt_dup_scope += 1
                    continue
                self._last_scope_key = scope_key

            if self._t_offset_us == 0:
                self._t_offset_us = hdr.timestamp_us
            t0 = (hdr.timestamp_us - self._t_offset_us) / 1_000_000.0

            # scope выбран -> окно 61 точки (раскладка [канал][выборка], 3 блока
            # по 61), скаляры удерживаются постоянными. Иначе — один отсчёт на
            # пакет по метке времени (логгер, частота = темп пакетов).
            nrows = sch.scope_n if has_scope else 1
            times = (t0 + idx61 * dt) if has_scope else np.array([t0], dtype=np.float64)

            values = np.empty((nrows, len(self._sel)), dtype=np.float32)
            for ci, key in enumerate(self._sel):
                v = sch.resolve(key, devices, system, scope)
                if isinstance(v, np.ndarray):
                    values[:, ci] = v * scope_scale
                else:
                    values[:, ci] = v

            put_drop_oldest(self._queue, (times, values))

        try:
            sock.close()
        except OSError:
            pass

    def _check_sequence(self, seq: int):
        if self._last_seq >= 0:
            expected = (self._last_seq + 1) & 0xFFFFFFFF
            if seq != expected:
                self._pkt_lost += (seq - expected) & 0xFFFFFFFF
        self._last_seq = seq

    def _drain_queue(self):
        self._drain_errors()
        while True:
            try:
                times, values = self._queue.get_nowait()
                self._emit(times, values)
            except queue.Empty:
                break

    # -- диагностика -----------------------------------------------------

    @property
    def telemetry(self) -> dict:
        """Снимок телеметрии устройств/системы из последнего пакета.
        Не идёт через _emit — отображение отдельная задача."""
        return self._telemetry

    @property
    def stats(self) -> dict:
        st = self._control.last_status if self._control else None
        return {
            'device_ip': self._config.device_ip,
            'pkt_ok': self._pkt_ok, 'pkt_err': self._pkt_err,
            'pkt_lost': self._pkt_lost, 'pkt_overflow': self._pkt_overflow,
            'pkt_dup_scope': self._pkt_dup_scope,
            'n_ch': self._n_ch,
            'device_status': st,
        }


# ---------------------------------------------------------------------------
# Диалог настройки
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
        QPushButton, QListWidget, QListWidgetItem, QDialogButtonBox, QGroupBox,
        QDoubleSpinBox, QSpinBox, QTreeWidget, QTreeWidgetItem, QAbstractItemView,
    )
except ImportError:  # headless (CI-тесты ставят только numpy) — диалог не создаётся
    class _NoQt:
        def __getattr__(self, name):
            raise RuntimeError("PySide6 не установлен: GUI FG-NET недоступен")
    QTimer = Qt = _NoQt()
    QDialog = QVBoxLayout = QHBoxLayout = QFormLayout = QLabel = QLineEdit = object
    QPushButton = QListWidget = QListWidgetItem = QDialogButtonBox = QGroupBox = object
    QDoubleSpinBox = QSpinBox = QTreeWidget = QTreeWidgetItem = QAbstractItemView = object


class FgNetConfigDialog(QDialog):
    def __init__(self, config: FgNetConfig | None = None, parent=None):
        super().__init__(parent)
        self._config = config or FgNetConfig()
        self.setWindowTitle('Настройка FG-NET источника')
        self.setMinimumWidth(440)

        self._schema = self._config.make_schema()
        self._schema_from_device = bool(self._config.schema)
        self._inbox_schema = None      # (ip, dict) от фонового fetch
        self._fetching_ip = None

        self._discovery = FgNetDiscovery(self._config.discovery_group,
                                         self._config.discovery_port)
        self._discovery.start()

        root = QVBoxLayout(self)

        # -- список найденных устройств -----------------------------------
        gb = QGroupBox('Устройства в сети')
        gv = QVBoxLayout(gb)
        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_pick)
        gv.addWidget(self._list)
        btn_search = QPushButton('Искать устройства')
        btn_search.clicked.connect(self._discovery.send_request)
        gv.addWidget(btn_search)
        root.addWidget(gb)

        # -- ручные поля -------------------------------------------------
        form = QFormLayout()
        self._ed_ip = QLineEdit(self._config.device_ip)
        self._ed_ip.setPlaceholderText('192.168.1.xxx (если multicast заблокирован)')
        form.addRow('IP устройства:', self._ed_ip)

        self._sb_rate = QSpinBox()
        self._sb_rate.setRange(50, 20000)
        self._sb_rate.setValue(self._config.scope_rate)
        self._sb_rate.setSuffix(' Гц')
        self._sb_rate.setToolTip('Частота дискретизации осциллографа на плате '
                                 'устройства (шкала времени графика).')
        form.addRow('Частота scope:', self._sb_rate)

        self._sb_scale = QDoubleSpinBox()
        self._sb_scale.setRange(0.0001, 1000.0)
        self._sb_scale.setDecimals(4)
        self._sb_scale.setValue(self._config.scope_scale)
        self._sb_scale.setToolTip('int16 → физические единицы. 0.1 — как в '
                                  'штатном осциллографе устройства; 1.0 — сырые отсчёты.')
        form.addRow('Масштаб scope:', self._sb_scale)

        self._sb_decim = QSpinBox()
        self._sb_decim.setRange(0, 255)
        self._sb_decim.setValue(self._config.telemetry_decim)
        self._sb_decim.setToolTip(
            'Делитель частоты по служебному кадру опроса устройства (~400 Гц).\n'
            'Пакет на каждый N-й -> частота ≈ 400/N Гц. 0 = как 1 (~400 Гц).\n'
            'Осциллограмма повторяется между захватами (+ ~10-14 Гц по захвату).\n'
            'ВНИМАНИЕ: высокая частота (N<4) нагружает HTTP-сервер устройства.')
        self._sb_decim.valueChanged.connect(lambda *_: self._update_hint())
        form.addRow('Прореживание:', self._sb_decim)
        root.addLayout(form)

        # -- выбор сигналов для записи ----------------------------------
        gb2 = QGroupBox('Сигналы для записи')
        gv2 = QVBoxLayout(gb2)
        self._lbl_schema = QLabel()
        self._lbl_schema.setStyleSheet('color:#888;')
        gv2.addWidget(self._lbl_schema)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QAbstractItemView.NoSelection)
        self._tree.setUniformRowHeights(True)
        self._build_signal_tree(set(self._config.selected()))
        self._update_schema_label()
        gv2.addWidget(self._tree)
        row = QHBoxLayout()
        b_none = QPushButton('Снять все')
        b_def = QPushButton('По умолчанию')
        b_none.clicked.connect(lambda: self._set_checks(set()))
        b_def.clicked.connect(lambda: self._set_checks(set(self._schema.default_channels())))
        row.addWidget(b_none)
        row.addWidget(b_def)
        row.addStretch()
        gv2.addLayout(row)
        self._lbl_hint = QLabel()
        self._lbl_hint.setStyleSheet('color:#888;')
        gv2.addWidget(self._lbl_hint)
        self._tree.itemChanged.connect(lambda *_: self._update_hint())
        self._update_hint()
        root.addWidget(gb2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._refresh = QTimer(self)
        self._refresh.timeout.connect(self._refresh_list)
        self._refresh.start(500)
        self._discovery.send_request()
        if self._config.device_ip:          # обновить схему известного устройства
            self._request_schema(self._config.device_ip)

    # -- дерево сигналов (строится из схемы) --------------------------

    def _build_signal_tree(self, checked: set[str]):
        self._tree.clear()
        for title, rows in self._schema.groups():
            grp = QTreeWidgetItem(self._tree, [title])
            grp.setFlags(Qt.ItemIsEnabled)
            grp.setExpanded(title == self._schema.sys_group or 'сцил' in title)
            for key, label in rows:
                it = QTreeWidgetItem(grp, [label])
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setData(0, Qt.UserRole, key)
                it.setCheckState(0, Qt.Checked if key in checked else Qt.Unchecked)

    def _update_schema_label(self):
        if self._schema_from_device:
            self._lbl_schema.setText('Схема каналов получена от устройства.')
        else:
            self._lbl_schema.setText('Схема каналов — встроенная (устройство не опрошено).')

    def _iter_leaves(self):
        for i in range(self._tree.topLevelItemCount()):
            grp = self._tree.topLevelItem(i)
            for j in range(grp.childCount()):
                yield grp.child(j)

    def _set_checks(self, keys: set[str]):
        for it in self._iter_leaves():
            it.setCheckState(0, Qt.Checked if it.data(0, Qt.UserRole) in keys else Qt.Unchecked)

    def _checked_keys(self) -> list[str]:
        return [it.data(0, Qt.UserRole) for it in self._iter_leaves()
                if it.checkState(0) == Qt.Checked]

    def _update_hint(self):
        keys = self._checked_keys()
        has_scope = any(k.startswith('scope.') for k in keys)
        d = max(1, self._sb_decim.value()) if hasattr(self, '_sb_decim') else 4
        rate = f'~{400 // d} Гц (прореж. {d})'
        if not keys:
            txt = 'Ничего не выбрано — запишутся каналы по умолчанию.'
        elif has_scope:
            txt = (f'{len(keys)} кан. Осциллограф обновляется ~10-14 Гц независимо от '
                   f'прореживания (повторные захваты отбрасываются).')
        else:
            txt = f'{len(keys)} кан. Логгер: 1 отсчёт/пакет, {rate}.'
        self._lbl_hint.setText(txt)

    # ------------------------------------------------------------------

    def _refresh_list(self):
        # применить схему, пришедшую из фонового fetch
        if self._inbox_schema is not None:
            ip, d = self._inbox_schema
            self._inbox_schema = None
            if ip == self._ed_ip.text().strip():
                try:
                    self._schema = FgNetSchema(d)
                    self._schema_from_device = True
                    keep = set(self._checked_keys()) or set(self._schema.default_channels())
                    self._build_signal_tree(keep)
                    self._update_schema_label()
                    self._update_hint()
                except Exception:
                    pass

        devs = self._discovery.devices
        cur_ip = self._selected_ip()
        self._list.clear()
        rows = sorted(devs.values(), key=lambda x: x.get('ip', ''))
        for d in rows:
            label = (f"{d.get('name', '?')}   {d['ip']}   "
                     f"fw {d.get('fw', '?')}   "
                     f"{'стрим' if d.get('state') else 'ожидание'}")
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, d)
            self._list.addItem(it)
            if d['ip'] == cur_ip:
                it.setSelected(True)
        # одно устройство и поле IP пустое — выбрать автоматически
        if len(rows) == 1 and not self._ed_ip.text().strip():
            self._list.setCurrentRow(0)

    def _selected_ip(self) -> str:
        it = self._list.currentItem()
        if it:
            return it.data(Qt.UserRole).get('ip', '')
        return self._ed_ip.text().strip()

    def _on_pick(self):
        it = self._list.currentItem()
        if not it:
            return
        d = it.data(Qt.UserRole)
        ip = d.get('ip', '')
        self._ed_ip.setText(ip)
        if d.get('max_sample_rate'):
            self._sb_rate.setValue(int(d['max_sample_rate']))
        self._request_schema(ip)

    def _request_schema(self, ip: str):
        """Фоновый GET_SCHEMA к устройству. Результат подхватит _refresh_list."""
        if not ip or ip == self._fetching_ip:
            return
        self._fetching_ip = ip
        cp = self._config.control_port

        def work():
            got = fetch_schema(ip, cp)
            if got is not None:
                self._inbox_schema = (ip, got)
            self._fetching_ip = None

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------

    def get_config(self) -> FgNetConfig:
        it = self._list.currentItem()
        dev_id = it.data(Qt.UserRole).get('device_id') if it else self._config.device_id
        keys = self._checked_keys() or self._schema.default_channels()
        schema = self._schema.to_dict() if self._schema_from_device else self._config.schema
        return FgNetConfig(
            device_id=dev_id,
            device_ip=self._ed_ip.text().strip(),
            control_port=self._config.control_port,
            data_port=self._config.data_port,
            discovery_port=self._config.discovery_port,
            discovery_group=self._config.discovery_group,
            scope_rate=self._sb_rate.value(),
            scope_scale=self._sb_scale.value(),
            telemetry_decim=self._sb_decim.value(),
            channels=keys,
            schema=schema,
            schema_crc=(zlib.crc32(json.dumps(schema).encode()) if schema else 0),
        )

    # то же имя, что у ComMCobsDialog — для единообразия вызова
    def get_fgnet_config(self) -> FgNetConfig:
        return self.get_config()

    def done(self, result):
        self._refresh.stop()
        self._discovery.stop()
        super().done(result)
