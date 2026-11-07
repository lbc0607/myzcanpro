"""流程图 layout (no Qt): turns a Flow into boxes and arrows with absolute coordinates.

Every lane is a vertical swim-lane (准备 first, then one per channel, left to right).  A step is a box; a
条件分支 is a diamond whose 是 / 否 branches run side by side and join again below; a 循环 is a frame around its
body with a return arrow.  Clicking is resolved by the view through `Shape.key` (node id, or "id:then" etc. for
the empty-branch placeholders / branch labels that select a child list).
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .flow import CONTAINER_KEYS, TYPE_LABEL, Flow, children_of, describe

BW, BH = 230, 58                 # step box
DW, DH = 230, 78                 # decision diamond
GAP, HGAP, PAD, HEAD = 30, 36, 14, 30
LANE_GAP = 40


@dataclass
class Shape:
    kind: str                    # box | diamond | frame | lane | group | bg | dot
    key: str                     # selection key ("" = not clickable)
    x: float
    y: float
    w: float
    h: float
    title: str = ""
    sub: str = ""
    node: str = ""               # node id (for colouring by run state), "" for decorations
    tag: str = ""                # node type, or branch label for groups


@dataclass
class Edge:
    pts: List[Tuple[float, float]]
    arrow: bool = True
    label: str = ""


@dataclass
class Layout:
    shapes: List[Shape] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    width: float = 0
    height: float = 0


def _width(ch: str) -> int:
    return 2 if ord(ch) > 0x2E80 else 1


def elide(text: str, units: int) -> str:
    """Shorten to `units` display columns (CJK counts 2), with an ellipsis."""
    if sum(_width(c) for c in text) <= units:
        return text
    out, n = "", 0
    for ch in text:
        if n + _width(ch) > units - 1:
            break
        out += ch
        n += _width(ch)
    return out + "…"


class _Block:
    """Laid-out part with its own origin; entry and exit are the top / bottom centre."""

    def __init__(self) -> None:
        self.w = 0.0
        self.h = 0.0
        self.shapes: List[Shape] = []
        self.edges: List[Edge] = []

    def place(self, dx: float, dy: float, into: "_Block") -> None:
        for s in self.shapes:
            into.shapes.append(Shape(s.kind, s.key, s.x + dx, s.y + dy, s.w, s.h, s.title, s.sub, s.node, s.tag))
        for e in self.edges:
            into.edges.append(Edge([(x + dx, y + dy) for x, y in e.pts], e.arrow, e.label))


def _box(n: dict) -> _Block:
    b = _Block()
    sub = describe(n)
    if not n.get("enabled", True):
        sub = "（已禁用） " + sub
    b.w, b.h = BW, BH
    b.shapes.append(Shape("box", n["id"], 0, 0, BW, BH, elide(n["name"], 30), elide(sub, 36), n["id"], n["type"]))
    return b


def _sequence(nodes: List[dict], group_key: str, label: str = "", empty_hint: str = "（空：选中后点「添加」）") -> _Block:
    out = _Block()
    if not nodes:
        out.w, out.h = BW, 36
        out.shapes.append(Shape("group", group_key, 0, 0, BW, 36, label or "", empty_hint, "", label))
        return out
    parts = [_node_block(n) for n in nodes]
    w = max(p.w for p in parts)
    y = 0.0
    prev_bottom = None
    for p in parts:
        x = (w - p.w) / 2
        p.place(x, y, out)
        if prev_bottom is not None:
            out.edges.append(Edge([(w / 2, prev_bottom), (w / 2, y)]))
        prev_bottom = y + p.h
        y += p.h + GAP
    out.w, out.h = w, y - GAP
    return out


def _node_block(n: dict) -> _Block:
    t = n["type"]
    if t == "if":
        return _if_block(n)
    if t == "loop":
        return _loop_block(n)
    return _box(n)


def _if_block(n: dict) -> _Block:
    then = _sequence(n["then"], f"{n['id']}:then", "是")
    other = _sequence(n["else"], f"{n['id']}:else", "否")
    colw = max(then.w, other.w, DW / 2 + 30)
    W = 2 * colw + HGAP
    b = _Block()
    cx = W / 2
    b.shapes.append(Shape("diamond", n["id"], cx - DW / 2, 0, DW, DH, elide(n["name"], 26), elide(describe(n), 30), n["id"], "if"))
    top = DH + GAP
    mid = DH / 2
    tx, ox = colw / 2, colw + HGAP + colw / 2
    then.place(tx - then.w / 2, top, b)
    other.place(ox - other.w / 2, top, b)
    b.edges.append(Edge([(cx - DW / 2, mid), (tx, mid), (tx, top)], True, "是"))
    b.edges.append(Edge([(cx + DW / 2, mid), (ox, mid), (ox, top)], True, "否"))
    bottom = top + max(then.h, other.h)
    join = bottom + GAP / 2
    for bx, blk in ((tx, then), (ox, other)):
        b.edges.append(Edge([(bx, top + blk.h), (bx, join), (cx, join)], False))
    b.shapes.append(Shape("dot", "", cx - 4, join - 4, 8, 8))
    b.w, b.h = W, join + 4
    return b


def _loop_block(n: dict) -> _Block:
    body = _sequence(n["body"], f"{n['id']}:body", "循环体")
    side = 26
    W = max(body.w + 2 * PAD + 2 * side, BW + 20)
    b = _Block()
    H = HEAD + PAD + body.h + PAD
    b.shapes.append(Shape("frame", n["id"], 0, 0, W, H, elide("↻ " + n["name"], 40), elide(describe(n), 44), n["id"], "loop"))
    body.place((W - body.w) / 2, HEAD + PAD, b)
    cx = W / 2
    b.edges.append(Edge([(cx, HEAD), (cx, HEAD + PAD)]))
    rx = W - PAD - side / 2                                  # return arrow on the right inside the frame
    b.edges.append(Edge([(cx + body.w / 2, HEAD + PAD + body.h), (rx + side / 2, HEAD + PAD + body.h),
                         (rx + side / 2, HEAD + PAD / 2), (cx + 12, HEAD + PAD / 2)], True, ""))
    b.w, b.h = W, H
    return b


def build(flow: Flow) -> Layout:
    lay = Layout()
    x = 0.0
    tallest = 0.0
    for lane in flow.nodes:
        t = lane["type"]
        title = "准备" if t == "setup" else lane["name"]
        sub = "" if t == "setup" else f"通道{lane.get('channel', 0)}"
        seq = _sequence(lane["children"], lane["id"] + ":children", "", "（空：选中泳道后点「添加」）")
        inner_w = max(seq.w, BW)
        w = inner_w + 2 * PAD
        head_h = 46
        h = head_h + PAD + seq.h + PAD
        lay.shapes.append(Shape("bg", "", x, 0, w, h, "", "", "", t))
        lay.shapes.append(Shape("lane", lane["id"], x + PAD, PAD, inner_w, head_h - PAD, elide(title, 30), sub, lane["id"], t))
        blk = _Block()
        seq.place((inner_w - seq.w) / 2, 0, blk)
        for s in blk.shapes:
            s.x += x + PAD
            s.y += head_h + PAD
        for e in blk.edges:
            e.pts = [(px + x + PAD, py + head_h + PAD) for px, py in e.pts]
        lay.shapes += blk.shapes
        lay.edges += blk.edges
        cx = x + w / 2
        lay.edges.append(Edge([(cx, head_h + PAD - 4 + 0), (cx, head_h + PAD)], False))
        x += w + LANE_GAP
        tallest = max(tallest, h)
    lay.width, lay.height = max(x - LANE_GAP, 0), tallest
    return lay


def find(lay: Layout, key: str) -> Optional[Shape]:
    for s in lay.shapes:
        if s.key == key:
            return s
    return None


def hit(lay: Layout, x: float, y: float) -> Optional[Shape]:
    """The clickable shape under (x, y); the smallest one wins so a step inside a loop frame beats the frame."""
    best = None
    for s in lay.shapes:
        if s.key and s.x <= x <= s.x + s.w and s.y <= y <= s.y + s.h:
            if best is None or s.w * s.h < best.w * best.h:
                best = s
    return best
