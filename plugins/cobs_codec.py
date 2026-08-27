"""COBS decode and CRC-16/CCITT — no Qt, safe for unit tests."""


def crc16(data: bytes | bytearray) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
        crc &= 0xFFFF
    return crc


def cobs_decode(data: bytes) -> bytes | None:
    result = bytearray()
    idx = 0
    n = len(data)
    while idx < n:
        code = data[idx]
        if code == 0:
            return None
        idx += 1
        end = idx + code - 1
        if end > n:
            return None
        result.extend(data[idx:end])
        idx = end
        if code != 0xFF and idx < n:
            result.append(0x00)
    return bytes(result)


# back-compat aliases used by COM plugins
_crc16 = crc16
_cobs_decode = cobs_decode
