import copy
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np

from fusion_offline import bus, pairs, requirements, router, stitch
from fusion_offline.eagle import UnverifiedGeometry


BOARD = '''<eagle><drawing><board><plain>
<wire x1="0" y1="0" x2="20" y2="0" width="0" layer="20"/>
<wire x1="20" y1="0" x2="20" y2="20" width="0" layer="20"/>
<wire x1="20" y1="20" x2="0" y2="20" width="0" layer="20"/>
<wire x1="0" y1="20" x2="0" y2="0" width="0" layer="20"/>
</plain><designrules><param name="layerSetup" value="(1*2*15*16)"/></designrules>
<libraries><library name="L"><packages><package name="RF"/>
<package name="PIN"><smd name="1" x="0" y="0" dx="0.5" dy="0.5" layer="1"/></package>
</packages></library></libraries><elements>
<element name="U1" library="L" package="RF" x="10" y="10" rot="R0"/>
<element name="J2" library="L" package="PIN" x="2" y="10"/>
<element name="J3" library="L" package="PIN" x="18" y="10"/>
</elements><signals><signal name="S"><contactref element="J2" pad="1"/>
<contactref element="J3" pad="1"/></signal><signal name="GND"/></signals></board></drawing></eagle>'''


def board(layer=41, points=((-2, -11), (2, -11), (2, 11), (-2, 11)), width=0):
    root = ET.fromstring(BOARD)
    polygon = ET.SubElement(root.find('.//package[@name="RF"]'), 'polygon', layer=str(layer), width=str(width))
    for x, y in points:
        ET.SubElement(polygon, 'vertex', x=str(x), y=str(y))
    return root


def grid(root):
    return router.Grid(root, 'S', .2, .1, .25, (0, 0), layers=(1, 2, 15, 16))


class PackageRestrictTest(unittest.TestCase):
    def test_package_polygon_blocks_inside_and_copper_width(self):
        obstacles = stitch.board_obstacles(board(width=.4))[0]
        self.assertTrue(any(o.net is None and o.distance(10, 10) < 0 for o in obstacles))
        self.assertTrue(any(o.net is None and o.distance(7.9, 10) < 0 for o in obstacles))
        self.assertFalse(any(o.net is None and o.distance(7, 10) < 0 for o in obstacles))

    def test_concave_restrict_does_not_fill_its_notch(self):
        root = board(points=((0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)))
        obstacles = [o for o in stitch.board_obstacles(root)[0] if o.net is None]
        self.assertTrue(any(o.distance(10.5, 12) < 0 for o in obstacles))
        self.assertFalse(any(o.distance(12, 12) < 0 for o in obstacles))
        g = grid(root)
        self.assertFalse(g.free(*g.ij(10.5, 12), 1))
        self.assertTrue(g.free(*g.ij(12, 12), 1))

    def test_rotation_and_mirror_swap_restrict_side(self):
        for rotation, point, blocked_layer in [('R90', (9.5, 11), 1), ('MR90', (10.5, 11), 16)]:
            with self.subTest(rotation=rotation):
                root = board(points=((0, 0), (2, 0), (2, 1), (0, 1)))
                root.find('.//element[@name="U1"]').set('rot', rotation)
                g = grid(root)
                self.assertFalse(g.free(*g.ij(*point), blocked_layer))
                self.assertTrue(g.free(*g.ij(*point), 16 if blocked_layer == 1 else 1))

    def test_top_bottom_and_via_only_restrict(self):
        for source_layer, blocked in [(41, {1}), (42, {16}), (43, set())]:
            with self.subTest(source_layer=source_layer):
                g = grid(board(layer=source_layer))
                i, j = g.ij(10, 10)
                self.assertEqual({layer for layer in g.layers if not g.free(i, j, layer)}, blocked)
                self.assertTrue(g.via_blocked[i, j])

    def test_plain_restrict_polygon_is_also_an_obstacle(self):
        root = board()
        package = root.find('.//package[@name="RF"]')
        polygon = package.find('polygon')
        package.remove(polygon)
        for vertex in polygon:
            vertex.set('x', str(float(vertex.get('x')) + 10))
            vertex.set('y', str(float(vertex.get('y')) + 10))
        root.find('.//board/plain').append(polygon)
        g = grid(root)
        self.assertFalse(g.free(*g.ij(10, 10), 1))

    def test_curves_nonfinite_degenerate_and_self_crossing_reject(self):
        for mutation in ('curve', 'nan', 'line', 'crossed', 'negative_width'):
            with self.subTest(mutation=mutation):
                root = board()
                polygon = root.find('.//package/polygon')
                if mutation == 'curve': polygon[0].set('curve', '90')
                if mutation == 'nan': polygon[0].set('x', 'nan')
                if mutation == 'line':
                    for vertex in polygon: vertex.set('x', '0')
                if mutation == 'crossed': polygon[1], polygon[2] = copy.deepcopy(polygon[2]), copy.deepcopy(polygon[1])
                if mutation == 'negative_width': polygon.set('width', '-.1')
                with self.assertRaises(UnverifiedGeometry):
                    stitch.board_obstacles(root)

    def test_single_route_pair_check_and_bus_cannot_cross_restrict(self):
        root = board()
        route = router.route(root, 'S', (2, 10), (1,), (18, 10), (1,),
                             layers=(1,), vias=False, step=.25, max_nodes=10000)
        self.assertTrue(route.problems)
        obstacles = stitch.board_obstacles(root)[0]
        self.assertTrue(pairs._check('S', [(1, [(2, 10), (18, 10)])], [], .2, .6, obstacles, .1))
        self.assertFalse(pairs._check('S', [(16, [(2, 10), (18, 10)])], [], .2, .6, obstacles, .1))
        planned = bus.plan_bus(root, ['S'], [(2, 10), (18, 10)], .5, .2, 1, {'S': [(2, 10), (18, 10)]})
        self.assertTrue(planned['problems'])

    def test_via_stitching_keeps_clear_of_package_restrict(self):
        plan = stitch.plan_stitching(board(layer=43), 'GND', pitch=2, clearance=.1)
        self.assertTrue(plan['vias'])
        self.assertFalse(any(7.7 < x < 12.3 for x, y in plan['vias']))

    def test_scalar_and_vector_distance_agree(self):
        obstacles = [o for o in stitch.board_obstacles(board())[0] if o.net is None]
        self.assertTrue(obstacles)
        xs, ys = np.array([7., 8., 10., 12., 13.]), np.full(5, 10.)
        for obstacle in obstacles:
            np.testing.assert_allclose(router._dist_field(obstacle, xs, ys), [obstacle.distance(x, y) for x, y in zip(xs, ys)])

    def test_restriction_is_not_reported_as_copper_inside_a_keepout_requirement(self):
        result = requirements._evaluate(board(), {'type': 'keepout', 'region_mm': [9, 9, 11, 11],
                                                 'layers': [1, 16], 'allowed_nets': []}, {}, {})
        self.assertEqual(result[0], 'pass')

    def test_routing_requirement_detects_restrict_crossing(self):
        root = board()
        ET.SubElement(root.find('.//signal[@name="S"]'), 'wire', x1='2', y1='10', x2='18', y2='10', layer='1', width='.2')
        result = requirements._evaluate(root, {'type': 'routing', 'net': 'S', 'layers': [1], 'clearance_mm': .1}, {}, {})
        self.assertEqual(result[0], 'fail')

    def test_vrestrict_routing_requirement_allows_through_hole_pads_but_not_vias(self):
        root = board(layer=43)
        package = root.find('.//package[@name="PIN"]')
        package.remove(package.find('smd'))
        ET.SubElement(package, 'pad', name='1', x='0', y='0', drill='.3', diameter='.6')
        root.find('.//element[@name="J2"]').set('x', '10')
        signal = root.find('.//signal[@name="S"]')
        ET.SubElement(signal, 'wire', x1='10', y1='10', x2='18', y2='10', layer='1', width='.2')
        rule = {'type': 'routing', 'net': 'S', 'layers': [1], 'clearance_mm': .1}
        self.assertEqual(requirements._evaluate(root, rule, {}, {})[0], 'pass')
        ET.SubElement(signal, 'via', x='11', y='10', drill='.3', diameter='.6', extent='1-16')
        self.assertEqual(requirements._evaluate(root, rule, {}, {})[0], 'fail')

    def test_render_accepts_restrict_geometry(self):
        from fusion_offline.render import render
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'restrict.png')
            render(board(), path)
            with open(path, 'rb') as image:
                self.assertEqual(image.read(8), b'\x89PNG\r\n\x1a\n')
