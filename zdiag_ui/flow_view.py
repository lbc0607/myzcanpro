"""流程图 view: draws a `flowlayout.Layout` in a QGraphicsView.  Read-only; clicking selects a step (shape key),
double-clicking asks the window to edit it.  Run state colours the step boxes by node id."""
from typing import Dict, Optional

from zdiag.flow import Flow
from zdiag.flowlayout import Layout, Shape, build, hit
from .qt import Qt, QtCore, QtGui, QtWidgets as W, Signal

FILL = {"running": "#D6EAF8", "info": "#D6EAF8", "ok": "#D5F5E3", "fail": "#F5B7B1", "skip": "#E5E7E9"}
LINE = {"running": "#2980B9", "info": "#2980B9", "ok": "#27AE60", "fail": "#C0392B", "skip": "#95A5A6"}
BASE_FILL, BASE_LINE, TEXT, MUTED, SEL = "#FFFFFF", "#7F8C8D", "#2C3E50", "#7F8C8D", "#F39C12"


class FlowView(W.QGraphicsView):
    picked = Signal(str)                      # shape key
    edited = Signal(str)

    def __init__(self) -> None:
        sc = W.QGraphicsScene()
        super().__init__(sc)
        self.scene_ = sc
        self.lay: Layout = Layout([], [], 0, 0)
        self.bodies: Dict[str, object] = {}    # node id -> body item (recoloured on run events)
        self.sel_item = None
        self.selected = ""
        self.setRenderHint(QtGui.QPainter.Antialiasing)

    # ------------------------------------------------------------------ drawing
    def show_flow(self, flow: Flow, state: Dict[str, tuple], selected: str = "") -> None:
        self.lay = build(flow)
        sc = self.scene_
        sc.clear()
        self.bodies, self.sel_item = {}, None
        pen = QtGui.QPen(QtGui.QColor(BASE_LINE))
        for e in self.lay.edges:
            for a, b in zip(e.pts, e.pts[1:]):
                sc.addLine(a[0], a[1], b[0], b[1], pen)
            if e.arrow and len(e.pts) >= 2:
                self._arrowhead(e.pts[-2], e.pts[-1], pen)
            if e.label and len(e.pts) >= 2:
                t = sc.addText(e.label)
                t.setDefaultTextColor(QtGui.QColor(MUTED))
                t.setPos(e.pts[0][0] + 4, e.pts[0][1] + 2)
        for s in self.lay.shapes:
            self._draw(s, state.get(s.node) if s.node else None)
        sc.setSceneRect(0, 0, self.lay.width, self.lay.height)
        self.select(selected)

    def _arrowhead(self, a, b, pen) -> None:
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / n, dy / n
        p1 = QtCore.QPointF(b[0], b[1])
        p2 = QtCore.QPointF(b[0] - 9 * ux + 4.5 * uy, b[1] - 9 * uy - 4.5 * ux)
        p3 = QtCore.QPointF(b[0] - 9 * ux - 4.5 * uy, b[1] - 9 * uy + 4.5 * ux)
        self.scene_.addPolygon(QtGui.QPolygonF([p1, p2, p3]), pen, QtGui.QBrush(QtGui.QColor(BASE_LINE)))

    def _colors(self, st):
        s = st[0] if st else ""
        return FILL.get(s, BASE_FILL), LINE.get(s, BASE_LINE)

    def _draw(self, s: Shape, st) -> None:
        sc = self.scene_
        fill, line = self._colors(st)
        if s.kind == "bg":
            sc.addRect(s.x, s.y, s.w, s.h, QtGui.QPen(QtGui.QColor("#D5D8DC")), QtGui.QBrush(QtGui.QColor("#F8F9F9")))
            return
        if s.kind == "dot":
            sc.addEllipse(s.x, s.y, s.w, s.h, QtGui.QPen(QtGui.QColor(BASE_LINE)), QtGui.QBrush(QtGui.QColor(BASE_LINE)))
            return
        if s.kind == "lane":
            it = sc.addRect(s.x, s.y, s.w, s.h, QtGui.QPen(QtGui.QColor("#2E86C1")), QtGui.QBrush(QtGui.QColor("#D6EAF8")))
            self._label(s, s.title, "", bold=True)
            self.bodies[s.node] = it
            return
        if s.kind == "group":
            pen = QtGui.QPen(QtGui.QColor("#B2BABB"))
            pen.setStyle(Qt.DashLine)
            sc.addRect(s.x, s.y, s.w, s.h, pen, QtGui.QBrush(QtGui.QColor("#FDFEFE")))
            self._label(s, s.title, s.sub, muted=True)
            return
        if s.kind == "frame":
            pen = QtGui.QPen(QtGui.QColor("#8E44AD"))
            pen.setStyle(Qt.DashLine)
            it = sc.addRect(s.x, s.y, s.w, s.h, pen, QtGui.QBrush(QtGui.QColor(0, 0, 0, 0)))
            t = sc.addText(s.title)
            t.setDefaultTextColor(QtGui.QColor("#8E44AD"))
            t.setPos(s.x + 6, s.y + 2)
            if s.node:
                self.bodies[s.node] = it
            return
        if s.kind == "diamond":
            cx, cy = s.x + s.w / 2, s.y + s.h / 2
            poly = QtGui.QPolygonF([QtCore.QPointF(cx, s.y), QtCore.QPointF(s.x + s.w, cy),
                                    QtCore.QPointF(cx, s.y + s.h), QtCore.QPointF(s.x, cy)])
            it = sc.addPolygon(poly, QtGui.QPen(QtGui.QColor(line)), QtGui.QBrush(QtGui.QColor(fill)))
        else:
            it = sc.addRect(s.x, s.y, s.w, s.h, QtGui.QPen(QtGui.QColor(line)), QtGui.QBrush(QtGui.QColor(fill)))
        it.setToolTip(self._tip(s, st))
        self._label(s, s.title, s.sub)
        if s.node:
            self.bodies[s.node] = it

    def _label(self, s: Shape, title: str, sub: str, bold: bool = False, muted: bool = False) -> None:
        t = self.scene_.addText(title)
        t.setDefaultTextColor(QtGui.QColor(MUTED if muted else TEXT))
        t.setPos(s.x + 8, s.y + (4 if sub else s.h / 2 - 11))
        if sub:
            u = self.scene_.addText(sub)
            u.setDefaultTextColor(QtGui.QColor(MUTED))
            u.setPos(s.x + 8, s.y + 26)

    @staticmethod
    def _tip(s: Shape, st) -> str:
        return "\n".join(x for x in (s.title, s.sub, (st[0] + ": " + st[1]) if st else "") if x)

    # ------------------------------------------------------------------ run state / selection
    def set_state(self, node: str, st: Optional[tuple]) -> None:
        it = self.bodies.get(node)
        sh = next((s for s in self.lay.shapes if s.node == node and s.kind in ("box", "diamond")), None)
        if it is None or sh is None:
            return
        fill, line = self._colors(st)
        it.setBrush(QtGui.QBrush(QtGui.QColor(fill)))
        it.setPen(QtGui.QPen(QtGui.QColor(line)))
        it.setToolTip(self._tip(sh, st))

    def select(self, key: str) -> None:
        self.selected = key
        if self.sel_item is not None:
            self.scene_.removeItem(self.sel_item)
            self.sel_item = None
        s = next((x for x in self.lay.shapes if x.key == key), None) if key else None
        if s is None:
            return
        pen = QtGui.QPen(QtGui.QColor(SEL))
        pen.setWidth(3)
        self.sel_item = self.scene_.addRect(s.x - 3, s.y - 3, s.w + 6, s.h + 6, pen, QtGui.QBrush(QtGui.QColor(0, 0, 0, 0)))
        self.ensureVisible(s.x, s.y, s.w, s.h)

    # ------------------------------------------------------------------ mouse (hit-tested on the layout, not on items)
    def _key_at(self, ev) -> str:
        p = self.mapToScene(ev.pos())
        s = hit(self.lay, p.x(), p.y())
        return s.key if s else ""

    def mousePressEvent(self, ev) -> None:
        k = self._key_at(ev)
        if k:
            self.select(k)
            self.picked.emit(k)

    def mouseDoubleClickEvent(self, ev) -> None:
        k = self._key_at(ev)
        if k:
            self.select(k)
            self.picked.emit(k)
            self.edited.emit(k)
