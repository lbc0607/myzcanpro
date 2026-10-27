"""Single import point for the Qt binding (PyQt5 first, PySide2 as fallback)."""
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    Signal = QtCore.pyqtSignal
    BINDING = "PyQt5"
except ImportError:                                        # pragma: no cover
    from PySide2 import QtCore, QtGui, QtWidgets
    Signal = QtCore.Signal
    BINDING = "PySide2"

Qt = QtCore.Qt
