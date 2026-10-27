"""Real-time log saving with line-count rotation (no Qt)."""
import os
import time


class LogSaver:
    def __init__(self) -> None:
        self.base = ""
        self.max_lines = 100000
        self.fp = None
        self.lines = 0
        self.part = 0

    @property
    def active(self) -> bool:
        return self.fp is not None

    def _path(self) -> str:
        root, ext = os.path.splitext(self.base)
        return self.base if self.part == 0 else f"{root}_{self.part}{ext}"

    def start(self, base_path: str, max_lines: int = 100000) -> str:
        self.stop()
        self.base, self.max_lines, self.lines, self.part = base_path, max(int(max_lines), 1), 0, 0
        os.makedirs(os.path.dirname(os.path.abspath(base_path)), exist_ok=True)
        self.fp = open(self._path(), "a", encoding="utf-8", buffering=1)
        return self._path()

    def write(self, line: str) -> None:
        if not self.fp:
            return
        if self.lines >= self.max_lines:                    # rotate
            self.fp.close()
            self.part += 1
            self.lines = 0
            self.fp = open(self._path(), "a", encoding="utf-8", buffering=1)
        self.fp.write(line + "\n")
        self.lines += 1

    def stop(self) -> None:
        if self.fp:
            self.fp.close()
        self.fp = None

    @staticmethod
    def default_name(app_dir: str, prefix: str) -> str:
        return os.path.join(app_dir, "logs", f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
