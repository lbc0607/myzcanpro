"""Log-file helpers shared by the log savers and the message recorder (no Qt)."""
import os
import time
from typing import Iterable

AUTO_PREFIXES = ("uds_", "flash_", "monitor_")
AUTO_EXTS = (".txt", ".csv")


def part_path(base: str, part: int) -> str:
    """base.ext, base_1.ext, base_2.ext ..."""
    root, ext = os.path.splitext(base)
    return base if part == 0 else f"{root}_{part}{ext}"


def auto_name(folder: str, prefix: str) -> str:
    return os.path.join(folder, f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.txt")


def purge_old(folder: str, days: int, prefixes: Iterable[str] = AUTO_PREFIXES, now: float = None) -> int:
    """Delete automatically named log files older than `days` days (0 = keep everything).  Only files this program
    names itself (prefix + extension) are touched, never anything else in the folder.  Returns the number deleted."""
    if days <= 0 or not os.path.isdir(folder):
        return 0
    limit = (time.time() if now is None else now) - days * 86400
    n = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(tuple(prefixes)) or not name.lower().endswith(AUTO_EXTS):
            continue
        p = os.path.join(folder, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < limit:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n
