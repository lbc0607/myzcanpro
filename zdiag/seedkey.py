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

from .isotp import IsoTpError
from .uds import NegativeResponse, UdsTimeout

# physical diagnostic address -> (line 2, line 3) of EcuSerialNum.txt (software numbers; editable in the settings)
DEFAULT_ID_MAP: Dict[int, Tuple[int, int]] = {
    0x7E2: (0, 0),
    0x7E3: (1, 2),
    0x785: (2, 4),
    0x645: (1, 3),
}
SERIAL_FILE = "EcuSerialNum.txt"


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


class SerialState:
    """What has been written to EcuSerialNum.txt during THIS run of the program (one process = one run).

    The ECU serial number (DID F18C) is read only before the first security access of the run; later unlocks (also of
    other ECUs) keep line 1 and only rewrite lines 2/3.  `cleanup()` deletes the files again on exit."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.done = False
        self.text = ""
        self.paths = set()

    def forget(self) -> None:
        with self.lock:
            self.done, self.text = False, ""

    def reset(self, directory: str = "", name: str = SERIAL_FILE) -> None:
        """New run: forget the serial and drop a file a previous (crashed) run may have left behind."""
        self.forget()
        if directory:
            self._remove(os.path.join(directory, name))

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def cleanup(self) -> None:
        with self.lock:
            for p in list(self.paths):
                self._remove(p)
            self.paths.clear()
            self.done, self.text = False, ""


SERIAL_STATE = SerialState()


class SerialAwareKey:
    """key_fn for UdsClient.security_access().  Before the seed is requested (`prepare`) it writes

        <workdir>/EcuSerialNum.txt
            line 1: ECU serial number - read with 22 <serial_did> only before the FIRST security access of the run
                    (a negative response gives an empty line 1); later unlocks keep it
            line 2: software number for 2-byte seeds   } looked up by the PHYSICAL address of the ECU being
            line 3: software number for 16-byte seeds  } unlocked (configurable table); the key DLL picks the line

    and then the DLL computes the key (it reads the file itself).  Unknown physical address -> error, never a guess
    (a wrong key would count against the ECU's attempt counter).
    """

    FILE = SERIAL_FILE

    def __init__(self, dll: Optional[ZlgKeyDll], id_map: Optional[Dict[int, Tuple[int, int]]] = None,
                 serial_did: int = 0xF18C, serial_format: str = "ascii", state: Optional[SerialState] = None,
                 file_name: str = SERIAL_FILE) -> None:
        self.dll = dll
        self.id_map = dict(DEFAULT_ID_MAP if id_map is None else id_map)
        self.serial_did = serial_did
        self.serial_format = serial_format           # "ascii" | "hex"
        self.state = SERIAL_STATE if state is None else state
        self.FILE = file_name or SERIAL_FILE
        self.last_serial: Optional[str] = None

    def _format_serial(self, raw: bytes) -> str:
        if self.serial_format == "hex":
            return raw.hex().upper()
        txt = raw.rstrip(b"\x00\xff ").lstrip(b"\x00").decode("ascii", errors="strict")
        if not txt or not txt.isprintable():
            raise SeedKeyError(f"serial number not printable ASCII: {raw.hex(' ')} "
                               "(use serial_format='hex' if the DLL expects hex)")
        return txt

    def numbers_for(self, phys: int) -> Tuple[int, int]:
        if phys not in self.id_map:
            raise SeedKeyError(f"物理地址 0x{phys:X} 没有配置 {self.FILE} 第2/3行编号，"
                               f"请在 设置 → 安全访问 的映射表里添加（已有: {', '.join(f'{k:X}' for k in self.id_map)}）")
        return self.id_map[phys]

    def _read_serial(self, uds):
        """(text, done): NRC -> ('', True); no answer -> ('', False) so that the next unlock tries again."""
        try:
            return self._format_serial(uds.read_did(self.serial_did)), True
        except NegativeResponse:
            return "", True
        except (UdsTimeout, IsoTpError):
            return "", False

    def lines_for(self, phys: int, serial_text: str) -> str:
        a, b = self.numbers_for(phys)
        return f"{serial_text}\n{a}\n{b}\n"

    def prepare(self, uds) -> None:
        phys = uds.tp.tx_id
        self.numbers_for(phys)                       # refuse before anything is sent / written
        st = self.state
        with st.lock:
            if not st.done:
                st.text, st.done = self._read_serial(uds)
            content = self.lines_for(phys, st.text)
            workdir = self.dll.workdir if self.dll is not None else os.getcwd()
            os.makedirs(workdir, exist_ok=True)
            path = os.path.join(workdir, self.FILE)
            with open(path, "w", newline="\n", encoding="ascii") as f:
                f.write(content)
            st.paths.add(path)
        self.last_serial = st.text

    def __call__(self, seed: bytes, level: int) -> bytes:
        return self.dll(seed, level)
