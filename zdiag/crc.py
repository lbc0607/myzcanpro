"""Generic table-driven CRC (width 8..32) with the usual presets.

Parameter model = the Rocksoft/"CRC catalogue" model, which is also what the
ZCANPro checksum tool shows: type, polynomial, init, xor-out, reflect-in, reflect-out.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, List


@dataclass
class CrcSpec:
    name: str = "CRC32"
    width: int = 32
    poly: int = 0x04C11DB7
    init: int = 0xFFFFFFFF
    xorout: int = 0xFFFFFFFF
    refin: bool = True
    refout: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CrcSpec":
        base = CrcSpec()
        for k in asdict(base):
            if k in d:
                setattr(base, k, d[k])
        base.width, base.poly = int(base.width), int(base.poly)
        base.init, base.xorout = int(base.init), int(base.xorout)
        base.refin, base.refout = bool(base.refin), bool(base.refout)
        return base

    @property
    def nbytes(self) -> int:
        return (self.width + 7) // 8


PRESETS: Dict[str, CrcSpec] = {s.name: s for s in [
    CrcSpec("CRC32", 32, 0x04C11DB7, 0xFFFFFFFF, 0xFFFFFFFF, True, True),
    CrcSpec("CRC32/MPEG-2", 32, 0x04C11DB7, 0xFFFFFFFF, 0x00000000, False, False),
    CrcSpec("CRC32C", 32, 0x1EDC6F41, 0xFFFFFFFF, 0xFFFFFFFF, True, True),
    CrcSpec("CRC32/AUTOSAR", 32, 0xF4ACFB13, 0xFFFFFFFF, 0xFFFFFFFF, True, True),
    CrcSpec("CRC16/CCITT-FALSE", 16, 0x1021, 0xFFFF, 0x0000, False, False),
    CrcSpec("CRC16/XMODEM", 16, 0x1021, 0x0000, 0x0000, False, False),
    CrcSpec("CRC16/MODBUS", 16, 0x8005, 0xFFFF, 0x0000, True, True),
    CrcSpec("CRC16/ARC", 16, 0x8005, 0x0000, 0x0000, True, True),
    CrcSpec("CRC8", 8, 0x07, 0x00, 0x00, False, False),
]}
CUSTOM = "自定义"


def preset_names() -> List[str]:
    return list(PRESETS) + [CUSTOM]


def _reflect(v: int, width: int) -> int:
    r = 0
    for _ in range(width):
        r = (r << 1) | (v & 1)
        v >>= 1
    return r


_TABLES: Dict[tuple, List[int]] = {}


def _table(width: int, poly: int, refin: bool) -> List[int]:
    key = (width, poly, refin)
    t = _TABLES.get(key)
    if t is not None:
        return t
    t = []
    if refin:
        rp = _reflect(poly, width)
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ rp if c & 1 else c >> 1
            t.append(c)
    else:
        top, mask = 1 << (width - 1), (1 << width) - 1
        for i in range(256):
            c = i << (width - 8)
            for _ in range(8):
                c = ((c << 1) ^ poly) & mask if c & top else (c << 1) & mask
            t.append(c)
    _TABLES[key] = t
    return t


def crc_calc(spec: CrcSpec, data: bytes) -> int:
    w = spec.width
    if not 8 <= w <= 32:
        raise ValueError("CRC width must be 8..32")
    mask = (1 << w) - 1
    tbl = _table(w, spec.poly & mask, spec.refin)
    if spec.refin:
        c = _reflect(spec.init & mask, w)
        for b in data:
            c = tbl[(c ^ b) & 0xFF] ^ (c >> 8)
        res = c if spec.refout else _reflect(c, w)
    else:
        c = spec.init & mask
        sh = w - 8
        for b in data:
            c = tbl[((c >> sh) ^ b) & 0xFF] ^ ((c << 8) & mask)
        res = _reflect(c, w) if spec.refout else c
    return (res ^ spec.xorout) & mask


def crc_bytes(spec: CrcSpec, data: bytes, byteorder: str = "big") -> bytes:
    return crc_calc(spec, data).to_bytes(spec.nbytes, byteorder)
