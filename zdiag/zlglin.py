"""LinMaster on the ZLG CAN(FD) box's LIN channel (zlgcan.dll).

NOT IMPLEMENTED YET: the ctypes layouts of the LIN structures (ZCAN_LIN_INIT_CONFIG / LIN message / publish table) must be
copied from the zlgcan.h / zlgcan.py of the SDK that ships with the user's device; guessing them would corrupt memory.
Once they are available, implement `ZlgLinMaster(LinMaster)` here (send_frame / read_frame) and return it from open_zlg()."""
from .lin import LinError, LinMaster


def open_zlg(dll_path: str, device: str, channel: int, baud: int = 19200) -> LinMaster:
    raise LinError("周立功 LIN 通道的底层绑定还没有接入：需要设备 SDK 里 zlgcan.h 中 LIN 相关结构体定义（见 zdiag/zlglin.py）。"
                   "目前可用 kind=sim 做模拟演练")
