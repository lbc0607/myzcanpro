"""Runs the project's test modules (explicit list on purpose)."""
import os
import sys
import unittest

for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
MODULES = ["test_all", "test_sa", "test_engine", "test_devmgr", "test_monitor", "test_send", "test_ui"]

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    if os.environ.get("GITHUB_ACTIONS"):                  # failures as annotations (raw CI logs may be unreadable)
        for t, tb in list(res.failures) + list(res.errors):
            msg = (str(t) + "\n" + tb)[-1200:]
            print("::error title=test::" + msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
    sys.exit(0 if res.wasSuccessful() else 1)
