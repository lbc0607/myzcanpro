"""A small *stateful* fake of the PyQt5 subset used by zdiag_ui, so the GUI code can be executed
(constructors, signal wiring, slots, user flows) in an environment without Qt.

It does NOT validate real Qt behaviour/layout - it catches Python-level mistakes (NameError,
wrong attribute on our own objects, bad logic in handlers).  Real rendering is verified on the
Windows machine.  install() puts fake PyQt5.* modules into sys.modules.
"""
import sys
import types

SIGNAL_NAMES = {
    "clicked", "toggled", "textChanged", "textEdited", "currentIndexChanged", "currentTextChanged",
    "valueChanged", "stateChanged", "itemChanged", "itemSelectionChanged", "currentItemChanged",
    "itemClicked", "itemDoubleClicked", "cellChanged", "currentChanged", "finished", "started",
    "accepted", "rejected", "returnPressed", "editingFinished", "activated", "triggered",
    "timeout", "cellClicked", "currentCellChanged", "currentRowChanged", "itemActivated",
    "cellDoubleClicked", "aboutToQuit",
}


class _Sig:
    def __init__(self):
        self._s = []

    def connect(self, f, *a):
        self._s.append(f)

    def disconnect(self, *a):
        self._s = []

    def emit(self, *a):
        import inspect
        for f in list(self._s):
            # PyQt5 drops surplus signal arguments for slots that do not accept them
            try:
                params = list(inspect.signature(f).parameters.values())
            except (TypeError, ValueError):
                f(*a)
                continue
            if any(p.kind == p.VAR_POSITIONAL for p in params):
                f(*a)
            else:
                n = len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
                f(*a[:n])


class _U:
    """Universal sink: any attribute / call / operator works and does nothing."""
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _U()
    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        return _U()
    def __iter__(self): return iter(())
    def __bool__(self): return True
    def __int__(self): return 0
    def __or__(self, o): return self
    __ror__ = __or__
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Meta(type):
    def __getattr__(cls, n):                      # enum-like constants: QAbstractItemView.SelectRows
        if n[:1].isupper():
            return 0
        raise AttributeError(n)


class pyqtSignal:
    def __init__(self, *types):
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        key = "_sig_" + self.name
        if key not in obj.__dict__:
            obj.__dict__[key] = _Sig()
        return obj.__dict__[key]


class QObject(metaclass=_Meta):
    def __init__(self, *a, **k):
        self._parent = a[0] if a and isinstance(a[0], QObject) else None
        self._enabled = True
        self._visible = False
        self._props = {}
        self._tooltip = ""

    def __getattr__(self, n):
        if n.startswith("__") or n in ("_parent", "_enabled", "_visible", "_props"):
            raise AttributeError(n)
        if n in SIGNAL_NAMES:
            s = _Sig()
            self.__dict__[n] = s
            return s
        return _U()

    def setEnabled(self, v): self._enabled = bool(v)
    def isEnabled(self): return self._enabled
    def setVisible(self, v): self._visible = bool(v)
    def isVisible(self): return self._visible
    def show(self): self._visible = True
    def hide(self): self._visible = False
    def close(self):
        self._visible = False
        ce = _FakeEvent()
        if hasattr(self, "closeEvent"):
            self.closeEvent(ce)
        return True
    def parent(self): return self._parent
    def setObjectName(self, n): self._props["objectName"] = n
    def setProperty(self, k, v): self._props[k] = v
    def setToolTip(self, t): self._tooltip = t
    def deleteLater(self): pass


class _FakeEvent:
    def __init__(self): self.accepted = None
    def accept(self): self.accepted = True
    def ignore(self): self.accepted = False


class QWidget(QObject):
    pass


class QMainWindow(QWidget):
    def setCentralWidget(self, w): self._central = w
    def setWindowTitle(self, t): self._title = t
    def windowTitle(self): return getattr(self, "_title", "")


class QDialog(QWidget):
    next_result = 1

    def setWindowTitle(self, t): self._title = t
    def exec_(self): return QDialog.next_result
    def accept(self): self.accepted.emit(); return None
    def reject(self): self.rejected.emit(); return None


class QApplication(QObject):
    _inst = None

    def __init__(self, *a):
        super().__init__()
        QApplication._inst = self

    @staticmethod
    def instance(): return QApplication._inst
    @staticmethod
    def setAttribute(*a): pass
    def exec_(self): return 0
    def setStyleSheet(self, s): self._qss = s
    def setStyle(self, s): pass
    def setFont(self, f): pass


class QLabel(QWidget):
    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text = text if isinstance(text, str) else ""
    def text(self): return self._text
    def setText(self, t): self._text = str(t)


class QPushButton(QWidget):
    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text = text if isinstance(text, str) else ""
        self._checkable, self._checked = False, False
    def text(self): return self._text
    def setText(self, t): self._text = str(t)
    def setCheckable(self, v): self._checkable = v
    def isChecked(self): return self._checked
    def setChecked(self, v):
        if self._checkable and bool(v) != self._checked:
            self._checked = bool(v)
            self.toggled.emit(self._checked)
    def click(self):
        if not self._enabled:
            return
        if self._checkable:
            self._checked = not self._checked
            self.toggled.emit(self._checked)
        self.clicked.emit(self._checked)


class QCheckBox(QWidget):
    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text, self._checked = (text if isinstance(text, str) else ""), False
    def text(self): return self._text
    def setText(self, t): self._text = str(t)
    def isChecked(self): return self._checked
    def setChecked(self, v):
        if bool(v) != self._checked:
            self._checked = bool(v)
            self.toggled.emit(self._checked)
            self.stateChanged.emit(2 if self._checked else 0)
    def click(self): self.setChecked(not self._checked)


class QLineEdit(QWidget):
    def __init__(self, text="", *a):
        super().__init__(*a)
        self._text = text if isinstance(text, str) else ""
        self._ro = False
    def text(self): return self._text
    def setText(self, t):
        t = str(t)
        if t != self._text:
            self._text = t
            self.textChanged.emit(t)
    def setReadOnly(self, v): self._ro = v
    def isReadOnly(self): return self._ro
    def clear(self): self.setText("")
    def user_type(self, t):                       # test helper: like typing + Enter
        self.setText(t); self.textEdited.emit(t); self.editingFinished.emit()


class QSpinBox(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._v, self._min, self._max = 0, 0, 99
    def value(self): return self._v
    def setValue(self, v):
        v = max(self._min, min(self._max, int(v)))
        if v != self._v:
            self._v = v
            self.valueChanged.emit(v)
    def setRange(self, a, b): self._min, self._max = a, b
    def setMinimum(self, a): self._min = a
    def setMaximum(self, b): self._max = b


class QProgressBar(QSpinBox):
    def setMaximum(self, b): self._max = b
    def maximum(self): return self._max


class QComboBox(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._items, self._idx = [], -1
    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._idx < 0:
            self._idx = 0
    def addItems(self, texts):
        for t in texts:
            self.addItem(t)
    def clear(self): self._items, self._idx = [], -1
    def count(self): return len(self._items)
    def currentIndex(self): return self._idx
    def setCurrentIndex(self, i):
        if i != self._idx and -1 <= i < len(self._items):
            self._idx = i
            self.currentIndexChanged.emit(i)
            self.currentTextChanged.emit(self.currentText())
            self.activated.emit(i)
    def currentText(self): return self._items[self._idx][0] if 0 <= self._idx < len(self._items) else ""
    def currentData(self, *a): return self._items[self._idx][1] if 0 <= self._idx < len(self._items) else None
    def itemText(self, i): return self._items[i][0]
    def itemData(self, i, *a): return self._items[i][1]
    def findText(self, t, *a):
        for i, (x, _) in enumerate(self._items):
            if x == t:
                return i
        return -1
    def findData(self, d, *a):
        for i, (_, x) in enumerate(self._items):
            if x == d:
                return i
        return -1
    def setCurrentText(self, t):
        i = self.findText(t)
        if i >= 0:
            self.setCurrentIndex(i)
        elif getattr(self, "_editable", False):
            self._items.append((t, None)); self.setCurrentIndex(len(self._items) - 1)
    def setEditable(self, v): self._editable = v


class QPlainTextEdit(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._lines = []
        self._max = 0
    def appendPlainText(self, t):
        self._lines.append(t)
        if self._max and len(self._lines) > self._max:
            self._lines = self._lines[-self._max:]
    def setPlainText(self, t): self._lines = t.split("\n") if t else []
    def toPlainText(self): return "\n".join(self._lines)
    def clear(self): self._lines = []
    def setMaximumBlockCount(self, n): self._max = n
    def setReadOnly(self, v): pass


class QGroupBox(QWidget):
    def __init__(self, title="", *a):
        super().__init__(*a)
        self._title = title if isinstance(title, str) else ""
    def title(self): return self._title


class QTabWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._tabs, self._cur = [], 0
    def addTab(self, w, text): self._tabs.append([w, text]); return len(self._tabs) - 1
    def count(self): return len(self._tabs)
    def currentIndex(self): return self._cur
    def setCurrentIndex(self, i):
        if i != self._cur:
            self._cur = i
            self.currentChanged.emit(i)
    def tabText(self, i): return self._tabs[i][1]
    def setTabText(self, i, t): self._tabs[i][1] = t
    def widget(self, i): return self._tabs[i][0]
    def removeTab(self, i):
        del self._tabs[i]
        self._cur = min(self._cur, len(self._tabs) - 1)


class QStackedWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._w, self._cur = [], 0
    def addWidget(self, w): self._w.append(w); return len(self._w) - 1
    def removeWidget(self, w):
        self._w.remove(w)
        self._cur = min(self._cur, max(len(self._w) - 1, 0))
    def setCurrentIndex(self, i): self._cur = i
    def currentIndex(self): return self._cur
    def currentWidget(self): return self._w[self._cur] if self._w else None
    def count(self): return len(self._w)
    def widget(self, i): return self._w[i]


class _Item:
    """Shared item behaviour: text per column, data per role, check state, flags, colours."""
    def __init__(self, text=""):
        self._text = {0: text if isinstance(text, str) else ""}
        self._data, self._check, self._flags = {}, 0, 0
        self._bg = self._fg = None
        self._tooltip = ""
    def text(self, col=0): return self._text.get(col, "")
    def setText(self, a, b=None):
        if b is None:
            self._text[0] = str(a)
        else:
            self._text[a] = str(b)
        self._changed()
    def data(self, a, b=None):
        role = a if b is None else b                 # table item: data(role); tree item: data(col, role)
        if role == 0:
            return self._text.get(0, "")
        return self._data.get(role)
    def setData(self, a, b, c=None):
        if c is None:                              # QTableWidgetItem.setData(role, value)
            self._data[a] = b
        else:                                      # QTreeWidgetItem.setData(col, role, value)
            self._data[b] = c
    def setCheckState(self, *a):
        v = a[-1]
        if v != self._check:
            self._check = v
            self._changed()
    def checkState(self, *a): return self._check
    def setFlags(self, f): self._flags = f
    def flags(self): return self._flags
    def setBackground(self, *a): self._bg = a[-1]
    def setForeground(self, *a): self._fg = a[-1]
    def setToolTip(self, *a): self._tooltip = a[-1]
    def setSizeHint(self, *a): pass
    def _changed(self): pass


class QTableWidgetItem(_Item):
    def __init__(self, text="", *a):
        super().__init__(text)
        self._table = None
        self._flags = 33 | 2                      # enabled + editable + selectable by default
    def row(self): return self._table._locate(self)[0] if self._table else -1
    def column(self): return self._table._locate(self)[1] if self._table else -1
    def _changed(self):
        if self._table is not None and not self._table._blocked:
            r, c = self._table._locate(self)
            if r >= 0:
                self._table.itemChanged.emit(self)
                self._table.cellChanged.emit(r, c)


class QTableWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._rows, self._cols, self._cur, self._blocked = [], 0, -1, False
        self._widgets, self._headers = {}, []
        if a and isinstance(a[0], int) and len(a) >= 2:
            self.setRowCount(a[0]); self._cols = a[1]
    def _locate(self, item):
        for r, row in enumerate(self._rows):
            for c, it in row.items():
                if it is item:
                    return r, c
        return -1, -1
    def blockSignals(self, b):
        old = self._blocked
        self._blocked = bool(b)
        return old
    def setColumnCount(self, n): self._cols = n
    def columnCount(self): return self._cols
    def setRowCount(self, n):
        while len(self._rows) < n:
            self._rows.append({})
        del self._rows[n:]
    def rowCount(self): return len(self._rows)
    def insertRow(self, i): self._rows.insert(i, {})
    def removeRow(self, i):
        del self._rows[i]
        self._widgets = {k: v for k, v in self._widgets.items() if k[0] != i}
        self._cur = min(self._cur, len(self._rows) - 1)
    def setItem(self, r, c, it):
        it._table = self
        self._rows[r][c] = it
    def item(self, r, c): return self._rows[r].get(c) if 0 <= r < len(self._rows) else None
    def currentRow(self): return self._cur
    def setCurrentCell(self, r, c): self.selectRow(r)
    def selectRow(self, r):
        if r != self._cur:
            self._cur = r
            self.currentCellChanged.emit(r, 0, -1, 0)
            self.itemSelectionChanged.emit()
    def clearContents(self): self._rows = [{} for _ in self._rows]
    def clear(self): self._rows, self._cur = [], -1
    def setCellWidget(self, r, c, w): self._widgets[(r, c)] = w
    def cellWidget(self, r, c): return self._widgets.get((r, c))
    def setHorizontalHeaderLabels(self, labels): self._headers = list(labels)
    def scrollToBottom(self): pass
    def cell_texts(self, r): return [self._rows[r][c].text() if c in self._rows[r] else "" for c in range(self._cols)]


class QTreeWidgetItem(_Item):
    def __init__(self, a=None, *rest):
        super().__init__("")
        self._children, self._parent = [], None
        if isinstance(a, list):
            for i, t in enumerate(a):
                self._text[i] = t
        elif isinstance(a, str):
            self._text[0] = a
        elif isinstance(a, (QTreeWidgetItem, QTreeWidget)):
            if isinstance(a, QTreeWidgetItem):
                a.addChild(self)
            else:
                a.addTopLevelItem(self)
            if isinstance(rest[0] if rest else None, list):
                for i, t in enumerate(rest[0]):
                    self._text[i] = t
    def addChild(self, c): c._parent = self; self._children.append(c)
    def child(self, i): return self._children[i]
    def childCount(self): return len(self._children)
    def parent(self): return self._parent
    def setExpanded(self, v): pass
    def indexOfChild(self, c): return self._children.index(c)


class QTreeWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._top, self._cur = [], None
    def addTopLevelItem(self, it): it._parent = None; self._top.append(it)
    def topLevelItem(self, i): return self._top[i]
    def topLevelItemCount(self): return len(self._top)
    def clear(self): self._top, self._cur = [], None
    def currentItem(self): return self._cur
    def setCurrentItem(self, it):
        old = self._cur
        self._cur = it
        self.currentItemChanged.emit(it, old)
        self.itemClicked.emit(it, 0)
    def expandAll(self): pass
    def setHeaderHidden(self, v): pass
    def indexOfTopLevelItem(self, it): return self._top.index(it)


class QListWidgetItem(_Item):
    def __init__(self, text="", *a):
        super().__init__(text)


class QListWidget(QWidget):
    def __init__(self, *a):
        super().__init__(*a)
        self._items, self._cur = [], -1
    def addItem(self, it):
        if isinstance(it, str):
            it = QListWidgetItem(it)
        self._items.append(it)
    def insertItem(self, i, it): self._items.insert(i, it)
    def count(self): return len(self._items)
    def item(self, i): return self._items[i]
    def takeItem(self, i): return self._items.pop(i)
    def clear(self): self._items, self._cur = [], -1
    def currentRow(self): return self._cur
    def setCurrentRow(self, i):
        if i != self._cur:
            self._cur = i
            self.currentRowChanged.emit(i)
            if 0 <= i < len(self._items):
                self.currentItemChanged.emit(self._items[i], None)
    def currentItem(self): return self._items[self._cur] if 0 <= self._cur < len(self._items) else None


class QFileDialog(QWidget):
    next_path = ""
    @staticmethod
    def getOpenFileName(*a, **k): return (QFileDialog.next_path, "")
    @staticmethod
    def getSaveFileName(*a, **k): return (QFileDialog.next_path, "")
    @staticmethod
    def getExistingDirectory(*a, **k): return QFileDialog.next_path


class QMessageBox(QWidget):
    calls = []
    next_answer = 16384
    Yes, No, Ok, Cancel = 16384, 65536, 1024, 4194304
    @staticmethod
    def _rec(kind, a):
        QMessageBox.calls.append((kind, a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return QMessageBox.Ok
    @staticmethod
    def information(*a, **k): return QMessageBox._rec("info", a)
    @staticmethod
    def warning(*a, **k): return QMessageBox._rec("warn", a)
    @staticmethod
    def critical(*a, **k): return QMessageBox._rec("crit", a)
    @staticmethod
    def question(*a, **k):
        QMessageBox.calls.append(("question", a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
        return QMessageBox.next_answer


class QInputDialog(QWidget):
    next_int = None
    @staticmethod
    def getInt(parent, title, label, value=0, *a, **k):
        return (QInputDialog.next_int if QInputDialog.next_int is not None else value, True)


class QThread(QObject):
    def start(self):
        self._running = True
        self.started.emit()
        try:
            self.run()
        finally:
            self._running = False
            self.finished.emit()
    def isRunning(self): return getattr(self, "_running", False)
    def wait(self, *a): return True
    def run(self): pass
    def requestInterruption(self): pass


class QTimer(QObject):
    @staticmethod
    def singleShot(ms, f): f()
    def start(self, *a): pass
    def stop(self): pass


class QColor:
    def __init__(self, *a): self.a = a


class QBrush:
    def __init__(self, *a): self.a = a


class _QtNS(metaclass=_Meta):
    UserRole = 256
    Checked, Unchecked, PartiallyChecked = 2, 0, 1
    ItemIsEditable, ItemIsUserCheckable, ItemIsEnabled, ItemIsSelectable = 2, 16, 32, 1
    Horizontal, Vertical = 1, 2
    AlignLeft, AlignRight, AlignVCenter, AlignCenter = 1, 2, 128, 132
    DisplayRole, ForegroundRole, BackgroundRole = 0, 9, 8


def _make_module(name, explicit):
    m = types.ModuleType(name)
    m.__dict__.update(explicit)
    cache = {}

    def __getattr__(n):
        if n.startswith("__"):
            raise AttributeError(n)
        if n not in cache:
            cache[n] = _Meta(n, (QWidget,), {})
        return cache[n]
    m.__getattr__ = __getattr__
    return m


def install():
    core = _make_module("PyQt5.QtCore", dict(
        QObject=QObject, pyqtSignal=pyqtSignal, QThread=QThread, QTimer=QTimer, Qt=_QtNS,
        QSize=_U, QPoint=_U, pyqtSlot=lambda *a, **k: (lambda f: f)))
    gui = _make_module("PyQt5.QtGui", dict(QColor=QColor, QBrush=QBrush))
    widgets = _make_module("PyQt5.QtWidgets", {
        k: v for k, v in globals().items()
        if isinstance(v, type) and k.startswith("Q") and k not in ("QObject", "QThread", "QTimer", "QColor", "QBrush")})
    pkg = types.ModuleType("PyQt5")
    pkg.QtCore, pkg.QtGui, pkg.QtWidgets = core, gui, widgets
    for name, mod in (("PyQt5", pkg), ("PyQt5.QtCore", core), ("PyQt5.QtGui", gui), ("PyQt5.QtWidgets", widgets)):
        sys.modules[name] = mod
    return pkg
