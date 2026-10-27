"""Wrapper for the company's security-access key DLL (Vector-style GenerateKeyEx).

Most OEM seed&key DLLs export (cdecl):
    int GenerateKeyEx(const uint8_t* seed, uint32_t seedLen, uint32_t level,
                      const char* variant, uint8_t* key, uint32_t maxKeyLen,
                      uint32_t* actualKeyLen);          // 0 == OK
Yours may differ (e.g. `GenerateKey`, different arg order, stdcall). Adapt `_call`.
The DLL's bitness must equal the Python process's bitness (32-bit if zlgcan.dll is used).
"""
from __future__ import annotations
import ctypes as C


class SeedKeyError(RuntimeError):
    pass


class SeedKeyDll:
    def __init__(self, path: str, func: str = "GenerateKeyEx", variant: str = "",
                 stdcall: bool = False, max_key: int = 64, level_map=None) -> None:
        loader = C.WinDLL if stdcall else C.CDLL
        self.lib = loader(path)
        self.fn = getattr(self.lib, func)
        self.variant = variant.encode()
        self.max_key = max_key
        self.level_map = level_map or (lambda sub: sub)     # UDS sub-function -> DLL level

    def __call__(self, seed: bytes, level: int) -> bytes:
        s = (C.c_ubyte * len(seed))(*seed)
        key = (C.c_ubyte * self.max_key)()
        out = C.c_uint(0)
        self.fn.restype = C.c_int
        rc = self.fn(s, len(seed), self.level_map(level), self.variant,
                     key, self.max_key, C.byref(out))
        if rc != 0:
            raise SeedKeyError(f"key DLL returned {rc}")
        return bytes(key[:out.value])


def xor_demo_key(seed: bytes, level: int) -> bytes:
    """Toy algorithm used by the simulator only. NEVER use on a real ECU."""
    return bytes(b ^ 0xA5 for b in seed)[::-1]


# =============================================================================
# ZLGKey-style DLL (sa_demo interface) + ECUserialnumber.txt side-channel
# =============================================================================
import os
import sys
import threading
from typing import Dict, Optional, Tuple

# diagnostic CAN ID -> (line 2, line 3) of ECUserialnumber.txt
DEFAULT_ID_MAP: Dict[int, Tuple[int, int]] = {
    0x7E2: (0, 0),
    0x7E3: (1, 3),
    0x7E8: (2, 4),
}


class ZlgKeyDll:
    """Wraps   int ZLGKey(const uint8_t* seed, uint16_t seedLen, uint32_t level,
                          const char* variant, uint8_t* key, uint16_t* keyLen)   (cdecl)

    level   = security level 1,3,5... (the odd requestSeed sub-function)
    keyLen  = in: capacity of key buffer, out: actual key length
    returns 0 on success.

    The company DLL reads `ECUserialnumber.txt` from its working directory *while it runs*.
    Because we cannot see inside it, two defensive options are provided:
      reload=True   load the DLL fresh for every call and FreeLibrary afterwards, so it cannot
                    have cached a stale serial number from an earlier ECU/session.
      workdir=...   directory where the file lives and which is made the process CWD during the
                    call (the DLL may resolve the file relative to CWD, not to its own folder).
    """

    def __init__(self, path: str, workdir: Optional[str] = None, variant: str = "",
                 max_key: int = 64, reload: bool = True) -> None:
        self.path = os.path.abspath(path)
        self.workdir = os.path.abspath(workdir or os.getcwd())
        self.variant = variant.encode()
        self.max_key = max_key
        self.reload = reload
        self._lock = threading.Lock()
        self._lib = None
        if not reload:
            self._lib = self._load()

    def _load(self):
        lib = C.CDLL(self.path)                              # cdecl (verified on sa_demo.dll)
        lib.ZLGKey.restype = C.c_int
        lib.ZLGKey.argtypes = [C.POINTER(C.c_ubyte), C.c_ushort, C.c_uint, C.c_char_p,
                               C.POINTER(C.c_ubyte), C.POINTER(C.c_ushort)]
        return lib

    @staticmethod
    def _free(lib) -> None:
        if sys.platform == "win32":
            C.windll.kernel32.FreeLibrary.argtypes = [C.c_void_p]
            C.windll.kernel32.FreeLibrary(lib._handle)

    def __call__(self, seed: bytes, level: int) -> bytes:
        with self._lock:
            lib = self._load() if self.reload else self._lib
            old = os.getcwd()
            try:
                os.chdir(self.workdir)
                s = (C.c_ubyte * len(seed))(*seed)
                key = (C.c_ubyte * self.max_key)()
                klen = C.c_ushort(self.max_key)
                rc = lib.ZLGKey(s, len(seed), level, self.variant, key, C.byref(klen))
                if rc != 0:
                    raise SeedKeyError(f"ZLGKey returned {rc}")
                return bytes(key[:klen.value])
            finally:
                os.chdir(old)
                if self.reload:
                    self._free(lib)


class SerialAwareKey:
    """key_fn for UdsClient.security_access(): before requesting the seed it

        1. reads DID 0xF18C (ECU serial number) with service 0x22,
        2. writes  <workdir>/ECUserialnumber.txt :
                line 1 = serial number
                line 2 = id_map[diag id][0]
                line 3 = id_map[diag id][1]
        3. later computes the key with the DLL (which reads that file).

    The diagnostic ID is looked up as the tester's request ID first, then the ECU's response
    ID (your table mixes 7E2/7E3 request IDs with a 7E8 response ID, so both are accepted).
    Unknown ID -> error, never a guess.
    """

    def __init__(self, dll: ZlgKeyDll, id_map: Optional[Dict[int, Tuple[int, int]]] = None,
                 serial_did: int = 0xF18C, serial_format: str = "ascii") -> None:
        self.dll = dll
        self.id_map = dict(DEFAULT_ID_MAP if id_map is None else id_map)
        self.serial_did = serial_did
        self.serial_format = serial_format           # "ascii" | "hex"
        self.last_serial: Optional[str] = None

    FILE = "ECUserialnumber.txt"

    def _format_serial(self, raw: bytes) -> str:
        if self.serial_format == "hex":
            return raw.hex().upper()
        txt = raw.rstrip(b"\x00\xff ").lstrip(b"\x00").decode("ascii", errors="strict")
        if not txt or not txt.isprintable():
            raise SeedKeyError(f"serial number not printable ASCII: {raw.hex(' ')} "
                               "(use serial_format='hex' if the DLL expects hex)")
        return txt

    def lines_for(self, tx_id: int, rx_id: int, serial_raw: bytes) -> str:
        for ident in (tx_id, rx_id):
            if ident in self.id_map:
                a, b = self.id_map[ident]
                break
        else:
            raise SeedKeyError(f"no ECUserialnumber.txt mapping for diag IDs "
                               f"0x{tx_id:X}/0x{rx_id:X}; known: "
                               f"{[hex(k) for k in self.id_map]}")
        return f"{self._format_serial(serial_raw)}\n{a}\n{b}\n"

    def prepare(self, uds) -> None:
        raw = uds.read_did(self.serial_did)
        content = self.lines_for(uds.tp.tx_id, uds.tp.rx_id, raw)
        os.makedirs(self.dll.workdir, exist_ok=True)
        with open(os.path.join(self.dll.workdir, self.FILE), "w", newline="\n",
                  encoding="ascii") as f:
            f.write(content)
        self.last_serial = content.split("\n", 1)[0]

    def __call__(self, seed: bytes, level: int) -> bytes:
        return self.dll(seed, level)
