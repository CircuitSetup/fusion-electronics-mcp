"""Functional blocks expose their local circuit through connected wire geometry."""
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fusion_offline import design as D, sch_layout as L, symbols as S
from fusion_mcp import sch_blocks as B


def symbol(name, pins):
    return S.from_part_json({'deviceset': name, 'symbol': {'name': name, 'pins': [
        {'name': pin, 'pad': pin, 'side': side} for pin, side in pins]}})


def connected(drawing, net, points):
    # Trace the drawing independently of net names and segment grouping.
    lines = [(a, b) for n, path in drawing.wires if n == net for a, b in zip(path, path[1:])]
    def on(p, a, b):
        return (abs((p[0]-a[0])*(b[1]-a[1])-(p[1]-a[1])*(b[0]-a[0])) < 1e-5
                and min(a[0], b[0])-1e-5 <= p[0] <= max(a[0], b[0])+1e-5
                and min(a[1], b[1])-1e-5 <= p[1] <= max(a[1], b[1])+1e-5)
    seen = {points[0]}
    all_points = set(points) | {p for line in lines for p in line}
    while True:
        more = {p for a, b in lines if any(on(s, a, b) for s in seen)
                for p in all_points if on(p, a, b)}
        if more <= seen:
            return all(p in seen for p in points)
        seen |= more


class BlockWiringTest(unittest.TestCase):
    def context(self, external=()):
        geo = {'U1': symbol('AMP', [('A', 'left'), ('UNUSED', 'left'), ('B', 'left'), ('OUT', 'right')]),
               'R1': symbol('R', [('1', 'left'), ('2', 'right')]),
               'R2': symbol('R', [('1', 'left'), ('2', 'right')])}
        pad_net = {('U1', 'A'): 'FEEDBACK', ('U1', 'B'): 'FEEDBACK', ('U1', 'OUT'): 'OUTPUT',
                   ('R1', '1'): 'OUTPUT', ('R1', '2'): 'FEEDBACK',
                   ('R2', '1'): 'FEEDBACK', ('R2', '2'): 'GND'}
        supplies = {'gnd': symbol('GND', [('1', 'left')]), 'bar': symbol('VDD', [('1', 'left')])}
        return L.Ctx(geo, pad_net, {}, set(geo), 'U1', {'GND'}, {'GND'}, set(), set(external), supplies)

    def assert_net_connected(self, ctx, drawing, net):
        points = []
        for ref, (x, y, rot, mirror) in drawing.parts.items():
            for name, pin in ctx.geo[ref].pins.items():
                if ctx.pin_net(ref, name) == net:
                    points.append(D.transform(pin.x, pin.y, x, y, rot, mirror))
        self.assertTrue(connected(drawing, net, points), net)

    def test_feedback_and_nonadjacent_ic_pins_are_drawn_connected(self):
        ctx = self.context()
        drawing = L.layout_block(ctx)
        for net in ('FEEDBACK', 'OUTPUT'):
            self.assert_net_connected(ctx, drawing, net)
        self.assertEqual(drawing.labels, [])
        self.assertTrue(any(net == 'GND' for net, *_ in drawing.supplies))

    def test_external_signal_has_one_port_and_connected_local_parts(self):
        ctx = self.context(external={'OUTPUT'})
        drawing = L.layout_block(ctx)
        self.assert_net_connected(ctx, drawing, 'OUTPUT')
        self.assert_net_connected(ctx, drawing, 'FEEDBACK')
        self.assertEqual([net for net, *_ in drawing.labels], ['OUTPUT'])

    def test_local_wires_avoid_unrelated_pins_and_nets(self):
        ctx = self.context(external={'OUTPUT'})
        drawing = L.layout_block(ctx)
        checks = L.check(drawing, ctx.geo, ctx.pad_net, ctx.block, ctx.supply_geo)
        for key in ('wrong', 'missing', 'shorts', 'pin_on_wire', 'wire_touch', 'crossings'):
            self.assertFalse(checks[key], (key, checks[key]))

    def test_signal_wires_stay_clear_of_the_ic_outline(self):
        drawing = L.layout_block(self.context(external={'OUTPUT'}))
        # This fixture's U1 outline is x=-5.08..5.08, y=-5.08..5.08.
        for _, path in drawing.wires:
            for a, b in zip(path, path[1:]):
                if a[1] == b[1] and min(a[0], b[0]) < 5.08 and max(a[0], b[0]) > -5.08:
                    self.assertFalse(-5.08 <= a[1] <= 5.08, (a, b))
                if a[0] == b[0] and min(a[1], b[1]) < 5.08 and max(a[1], b[1]) > -5.08:
                    self.assertFalse(-5.08 <= a[0] <= 5.08, (a, b))

    def test_blocked_local_circuit_is_refused_instead_of_labeled(self):
        ctx = self.context()
        drawing = L.Drawing(parts={'U1': (0, 0, 0, False)},
                            wires=[('FEEDBACK', [(-7.62, 2.54), (-10.16, 2.54)]),
                                   ('FEEDBACK', [(-7.62, -2.54), (-10.16, -2.54)])],
                            boxes=[(-100, -100, 100, 100, 'obstacle')])
        with self.assertRaisesRegex(ValueError, 'U1.*local net FEEDBACK'):
            L._heal(ctx, drawing)

    def test_top_and_bottom_signal_pins_join_their_support_circuit(self):
        ctx = self.context()
        ctx.geo['U1'] = S.Geo('VERTICAL', {
            'A': S.Pin('A', 0, 7.62, 0, 1, 2.54, ['A']),
            'B': S.Pin('B', 0, -7.62, 0, -1, 2.54, ['B'])}, {'A': 'A', 'B': 'B'},
            [('rect', -2.54, -5.08, 2.54, 5.08)])
        ctx.block = {'U1', 'R2'}
        drawing = L.layout_block(ctx)
        self.assert_net_connected(ctx, drawing, 'FEEDBACK')
        self.assertEqual(drawing.labels, [])

    def test_two_blocks_share_labels_without_breaking_their_local_wires(self):
        ctx = self.context()
        ctx.geo['U2'] = symbol('INPUT', [('1', 'left')])
        nets = {}
        for (ref, pad), net in ctx.pad_net.items():
            nets.setdefault(net, []).append((ref, pad))
        nets['OUTPUT'].append(('U2', '1'))
        parts = {ref: {'footprint': 'TEST', 'value': ref, 'pads': list(geo.pad_pin)}
                 for ref, geo in ctx.geo.items()}
        block = B.BlockSchematic({'parts': parts, 'nets': nets}, {'TEST': 'TEST@LIB'})
        sheets, checks, notes, where = block.layout(ctx.geo, ctx.supply_geo)
        self.assertEqual(where, [('U1', 1), ('U2', 1)])
        drawing = sheets[1]
        def pin(ref, name):
            p = ctx.geo[ref].pins[name]
            return D.transform(p.x, p.y, *drawing.parts[ref])
        self.assertTrue(connected(drawing, 'OUTPUT', [pin('U1', 'OUT'), pin('R1', '1')]))
        self.assertFalse(connected(drawing, 'OUTPUT', [pin('U1', 'OUT'), pin('U2', '1')]))
        self.assertEqual([n for n, *_ in drawing.labels], ['OUTPUT', 'OUTPUT'])
        self.assertFalse(checks[1]['unlinked'])


class BlockReadbackTest(unittest.TestCase):
    def test_routed_crossing_is_split_and_gets_a_visible_junction(self):
        block = object.__new__(B.BlockSchematic)
        drawing = L.Drawing(wires=[('SIGNAL', [(0, 0), (20, 0)]),
                                  ('SIGNAL', [(10, -10), (10, 0), (10, 10)])])
        commands = block.wire_scripts({1: drawing})[0][1]
        self.assertIn("NET 'SIGNAL' (0 0) (10 0);", commands)
        self.assertIn("NET 'SIGNAL' (10 0) (20 0);", commands)
        self.assertIn('JUNCTION (10 0);', commands)

    def test_matching_pinrefs_cannot_hide_missing_or_wrong_layer_wires(self):
        root = ET.parse(Path(__file__).parent / 'fixtures/mini.sch').getroot()
        nets = root.find('.//sheet/nets'); nets.clear()
        net = ET.SubElement(nets, 'net', name='SIGNAL', **{'class': '0'})
        seg = ET.SubElement(net, 'segment')
        for ref in ('R1', 'R2'):
            ET.SubElement(seg, 'pinref', part=ref, gate='G$1', pin='P$1')
        block = object.__new__(B.BlockSchematic)
        block.pad_net = {('R1', '1'): 'SIGNAL', ('R2', '1'): 'SIGNAL'}
        block.geo = {}
        sheets = {1: L.Drawing(wires=[('SIGNAL', [(0, 0), (20, 0)])])}
        for layer in ('94', '91'):
            with self.subTest(layer=layer):
                for w in seg.findall('wire'): seg.remove(w)
                # Fusion may split a continuous planned wire into several pieces.
                for start, end in ((0, 10), (10, 20)):
                    ET.SubElement(seg, 'wire', x1=str(start), y1='0', x2=str(end), y2='0', width='0', layer=layer)
                result = block.verify(root, sheets)
                self.assertEqual(result['ok'], layer == '91')
                self.assertEqual(bool(result['wire_mismatch']), layer != '91')
        seg.remove(seg.findall('wire')[-1])
        self.assertFalse(block.verify(root, sheets)['ok'])


if __name__ == '__main__':
    unittest.main()
