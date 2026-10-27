"""Stylesheet imitating the ZCANPro look (light-blue buttons, blue selected tabs)."""

QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 10pt; }
QMainWindow, QDialog { background: #F0F0F0; }
QPushButton { background: #CDE3F9; border: 1px solid #8FAAD0; border-radius: 3px; padding: 3px 9px; min-height: 20px; }
QPushButton:hover { background: #BBD7F5; }
QPushButton:pressed, QPushButton:checked { background: #9CC2EC; }
QPushButton:disabled { color: #8A8A8A; background: #E6E9EC; border-color: #C4CCD6; }
QLineEdit, QSpinBox, QComboBox { border: 1px solid #9BB0CE; background: white; padding: 1px 4px; min-height: 22px; }
QLineEdit:read-only { background: #F6F6F6; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { background: #EDEDED; color: #9A9A9A; }
QComboBox::drop-down { border-left: 1px solid #9BB0CE; width: 22px; }
QTabWidget::pane { border: none; border-top: 1px solid #9BB0CE; }
QTabBar::tab { padding: 5px 14px; background: white; border: 1px solid #9BB0CE; border-bottom: none; margin-right: 1px; }
QTabBar::tab:selected { background: #4A7FBF; color: white; }
QHeaderView::section { background: #DCE4EE; border: none; padding: 5px 6px; }
QTableWidget, QTreeWidget, QListWidget, QPlainTextEdit { border: 1px solid #9BB0CE; background: white; }
QTableWidget { gridline-color: #E4E9F0; selection-background-color: #B6CDEE; selection-color: black; }
QTreeWidget::item:selected, QListWidget#svcList::item:selected { background: #B6CDEE; color: black; }
QTreeWidget::item { min-height: 24px; }
QGroupBox { border: 1px solid #9BB0CE; margin-top: 14px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 4px; }
QFrame#panel { border: 1px solid #9BB0CE; background: #F0F0F0; }
QProgressBar { border: 1px solid #9BB0CE; background: white; text-align: center; min-height: 16px; }
QProgressBar::chunk { background: #4A7FBF; }
QListWidget#stepList { background: #F0F0F0; border: 1px solid #9BB0CE; }
QListWidget#stepList::item { margin: 3px; padding: 8px 10px; }
"""

# step-card colours of the executor
COLOR_IDLE = "#6C8EBF"
COLOR_RUNNING = "#E6A23C"
COLOR_OK = "#5FAE5B"
COLOR_FAIL = "#D9534F"
