import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.flow import Flow
from zdiag.flowlayout import BW, build, elide, find

EX = os.path.join(os.path.dirname(__file__), "..", "examples", "示例流程.json")


def overlap(a, b):
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def inside(a, outer):
    return a.x >= outer.x and a.y >= outer.y and a.x + a.w <= outer.x + outer.w and a.y + a.h <= outer.y + outer.h


class Layout(unittest.TestCase):
    def deep_flow(self):
        f = Flow(); l = f.make("lane"); f.nodes.append(l)
        a = f.make("scan"); l["children"].append(a)
        i = f.make("if"); l["children"].append(i)
        i["then"] += [f.make("delay"), f.make("function")]
        lp = f.make("loop"); i["else"].append(lp)
        lp["body"] += [f.make("uds"), f.make("if")]
        lp["body"][1]["then"].append(f.make("delay"))
        l["children"].append(f.make("send"))
        l2 = f.make("lane", channel=1); f.nodes.append(l2)
        return f

    def test_no_overlaps_and_containment(self):
        for f in (Flow.load(EX), self.deep_flow()):
            L = build(f)
            leaves = [s for s in L.shapes if s.kind in ("box", "diamond", "group", "lane")]
            for i, a in enumerate(leaves):
                for b in leaves[i + 1:]:
                    self.assertFalse(overlap(a, b), (a.title, b.title))
            bgs = [s for s in L.shapes if s.kind == "bg"]
            for s in L.shapes:
                if s.kind != "bg":
                    self.assertTrue(any(inside(s, g) for g in bgs), s.title)
            for fr in [s for s in L.shapes if s.kind == "frame"]:                        # loop bodies stay inside their frame
                inner = [s for s in leaves if s is not fr and overlap(s, fr)]
                self.assertTrue(all(inside(s, fr) for s in inner), fr.title)
            self.assertGreater(L.width, 0); self.assertGreater(L.height, 0)
            for s in L.shapes: self.assertTrue(s.x >= 0 and s.y >= 0 and s.x + s.w <= L.width + 1 and s.y + s.h <= L.height + 1, s.title)

    def test_every_node_has_one_clickable_shape_and_groups_exist(self):
        f = self.deep_flow(); L = build(f)
        for n in f.walk():
            self.assertEqual(len([s for s in L.shapes if s.key == n["id"]]), 1, n["name"])
        i = [n for n in f.walk() if n["type"] == "if"][0]
        self.assertIsNone(find(L, i["id"] + ":then")); self.assertIsNone(find(L, i["id"] + ":else"))      # non-empty branches have no placeholder
        empty = [n for n in f.walk() if n["type"] == "if"][1]
        self.assertIsNotNone(find(L, empty["id"] + ":else"))                                                 # empty branch placeholder
        self.assertIsNotNone(find(L, f.lanes()[1]["id"] + ":children"))                                      # empty lane placeholder

    def test_flow_goes_top_down_and_branches_side_by_side(self):
        f = self.deep_flow(); L = build(f)
        lane = f.lanes()[0]; ids = [n["id"] for n in lane["children"]]
        ys = [find(L, i).y for i in ids]; self.assertEqual(ys, sorted(ys))
        i = lane["children"][1]
        t, o = find(L, i["then"][0]["id"]), find(L, i["else"][0]["id"])
        self.assertLess(t.x + t.w, o.x); self.assertAlmostEqual(t.y, find(L, i["else"][0]["id"]).y, delta=1)
        d = find(L, i["id"]); self.assertGreater(t.y, d.y + d.h)
        labels = sorted(e.label for e in L.edges if e.label); self.assertIn("是", labels); self.assertIn("否", labels)
        lanes = [s for s in L.shapes if s.kind == "lane"]; self.assertEqual([s.x for s in lanes], sorted(s.x for s in lanes))

    def test_edges_connect_consecutive_steps(self):
        f = Flow(); l = f.make("lane"); f.nodes.append(l)
        a, b = f.make("delay"), f.make("delay"); l["children"] += [a, b]
        L = build(f); sa, sb = find(L, a["id"]), find(L, b["id"])
        self.assertTrue(any(e.pts[0] == (sa.x + sa.w / 2, sa.y + sa.h) and e.pts[-1] == (sb.x + sb.w / 2, sb.y) for e in L.edges))

    def test_elide_and_disabled_marker(self):
        self.assertEqual(elide("abc", 10), "abc"); self.assertEqual(elide("软件扫描" * 10, 10), "软件扫描…")
        self.assertLessEqual(sum(2 if ord(c) > 0x2E80 else 1 for c in elide("动力域控制器扫描" * 5, 20)), 20)
        f = Flow(); l = f.make("lane"); f.nodes.append(l); d = f.make("delay", enabled=False); l["children"].append(d)
        self.assertIn("已禁用", find(build(f), d["id"]).sub)

    def test_empty_flow(self):
        L = build(Flow()); self.assertEqual((L.shapes, L.width), ([], 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
