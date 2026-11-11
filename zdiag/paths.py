"""Where the program's data lives (shared by the daemon, MCP server and function tools)."""
import os
from typing import Optional


def home_dir(home: Optional[str] = None) -> str:
    if home:
        return home
    if os.environ.get("CANVDS_HOME"):
        return os.environ["CANVDS_HOME"]
    from zdiag_ui.appctx import app_dir
    return app_dir()


def scan_dir(home: Optional[str] = None) -> str:
    return os.path.join(home_dir(home), "scan_data")
