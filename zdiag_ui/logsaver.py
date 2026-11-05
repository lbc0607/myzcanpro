"""Real-time log saving with rotation by size and by line count (no Qt)."""
import os

from zdiag.logfiles import auto_name, part_path


class LogSaver:
    def __init__(self) -> None:
        self.base = ""
        self.max_lines = 100000
        self.max_bytes = 0                                  # 0 = no size limit
        self.fp = None
        self.lines = 0
        self.size = 0
        self.part = 0

    @property
    def active(self) -> bool:
        return self.fp is not None

    def _path(self) -> str:
        return part_path(self.base, self.part)

    def _open(self) -> None:
        self.fp = open(self._path(), "a", encoding="utf-8", buffering=1)
        self.lines, self.size = 0, 0

    def start(self, base_path: str, max_lines: int = 100000, max_bytes: int = 0) -> str:
        self.stop()
        self.base, self.max_lines, self.max_bytes, self.part = base_path, max(int(max_lines), 1), max(int(max_bytes), 0), 0
        os.makedirs(os.path.dirname(os.path.abspath(base_path)), exist_ok=True)
        self._open()
        return self._path()

    def write(self, line: str) -> None:
        if not self.fp:
            return
        n = len(line.encode("utf-8")) + 1
        if self.lines and (self.lines >= self.max_lines or (self.max_bytes and self.size + n > self.max_bytes)):
            self.fp.close()                                 # rotate
            self.part += 1
            self._open()
        self.fp.write(line + "\n")
        self.lines += 1
        self.size += n

    def stop(self) -> None:
        if self.fp:
            self.fp.close()
        self.fp = None

    @staticmethod
    def default_name(folder: str, prefix: str) -> str:
        return auto_name(folder, prefix)
