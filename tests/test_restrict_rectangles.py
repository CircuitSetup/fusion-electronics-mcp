import unittest
import xml.etree.ElementTree as ET
from fusion_offline.stitch import board_obstacles
from fusion_offline.eagle import UnverifiedGeometry
from fusion_offline.router import Grid, _seg_clear


def board(shape, package=False):
    plain = '' if package else shape
    libraries = f'<libraries><library name="L"><packages><package name="P">{shape}</package></packages></library></libraries>' if package else ''
    elements = '<elements><element name="U1" library="L" package="P" x="10" y="10" rot="MR90"/></elements>' if package else ''
    return ET.fromstring(f'<eagle><drawing><board><plain>{plain}</plain>{libraries}{elements}<signals/><designrules><param name="layerSetup" value="(1*16)"/></designrules></board></drawing></eagle>')


def rect(layer=41, extra=''):
    return f'<rectangle x1="1" y1="2" x2="3" y2="4" layer="{layer}" {extra}/>'


def native(extra='restrictlayers="1"', layer=41):
    vertices = '<vertex x="1" y="2"/><vertex x="3" y="2"/><vertex x="3" y="4"/><vertex x="1" y="4"/>'
    return f'<polygonshape layer="{layer}" {extra}><polygonoutlineobjects>{vertices}</polygonoutlineobjects><polygonoutlinesegments>{vertices}<vertex x="1" y="2"/></polygonoutlinesegments></polygonshape>'


class RestrictRectanglesTest(unittest.TestCase):
    def test_legacy_layers_and_router(self):
        for layer, blocked in [(41, {1}), (42, {16}), (43, set())]:
            with self.subTest(layer=layer):
                root = board(rect(layer))
                before = ET.tostring(root)
                obstacle, = board_obstacles(root)[0]
                self.assertEqual(obstacle.layers, frozenset(blocked))
                self.assertEqual(obstacle.restrict_layer, layer)
                self.assertLess(obstacle.distance(2, 3), 0)
                grid = Grid(root, 'N', .2, .1, .5, (0, 0), window=(0, 0, 6, 6))
                for copper in (1, 16):
                    self.assertEqual(_seg_clear(grid, copper, (0, 3), (4, 3)), copper not in blocked)
                self.assertTrue(grid.via_blocked[4, 6])
                self.assertEqual(ET.tostring(root), before)

    def test_package_transform(self):
        obstacle, = board_obstacles(board(rect(), True))[0]
        self.assertEqual(obstacle.layers, frozenset({16}))
        self.assertEqual(obstacle.source_ref, 'U1')
        self.assertEqual(obstacle.bounds, (12, 11, 14, 13))

    def test_native_outline_and_explicit_layer_override(self):
        for shape in (native(), rect(42, 'restrictlayers="1"')):
            root = board(shape)
            before = ET.tostring(root)
            obstacle, = board_obstacles(root)[0]
            self.assertEqual(obstacle.layers, frozenset({1}))
            self.assertEqual(obstacle.bounds, (1, 2, 3, 4))
            self.assertEqual(ET.tostring(root), before)
        obstacle, = board_obstacles(board(native(), True))[0]
        self.assertEqual(obstacle.layers, frozenset({16}))
        self.assertEqual(obstacle.bounds, (12, 11, 14, 13))

    def test_expanded_native_outline_matches_legacy_bounds(self):
        # Measured export conversion: the native contour includes the old polygon stroke.
        old = '<polygon width=".1524" layer="41"><vertex x="23.9238" y="27.6738"/><vertex x="-23.9238" y="27.6738"/><vertex x="-23.9238" y="6.8262"/><vertex x="23.9238" y="6.8262"/></polygon>'
        vertices = '<vertex x="24" y="27.75"/><vertex x="-24" y="27.75"/><vertex x="-24" y="6.75"/><vertex x="24" y="6.75"/>'
        new = f'<polygonshape layer="41" restrictlayers="1"><polygonoutlineobjects>{vertices}</polygonoutlineobjects><polygonoutlinesegments>{vertices}<vertex x="24" y="27.75"/></polygonoutlinesegments></polygonshape>'
        for package in (False, True):
            before, = board_obstacles(board(old, package))[0]
            after, = board_obstacles(board(new, package))[0]
            self.assertEqual(before.layers, after.layers)
            for a, b in zip(before.bounds, after.bounds):
                self.assertAlmostEqual(a, b)

    def test_combined_outer_mask_preserves_both_surfaces_and_package_transform(self):
        for shape in (native('restrictlayers="1,16"'), native('restrictlayers="16,1"'),
                      rect(42, 'restrictlayers="1,16"')):
            for package in (False, True):
                with self.subTest(shape=shape, package=package):
                    root = board(shape, package)
                    root.find('.//param[@name="layerSetup"]').set('value', '(1*2*15*16)')
                    before = ET.tostring(root)
                    obstacle, = board_obstacles(root)[0]
                    self.assertEqual(obstacle.layers, frozenset({1, 16}))
                    self.assertEqual(obstacle.source_ref, 'U1' if package else None)
                    self.assertEqual(obstacle.bounds, (12, 11, 14, 13) if package else (1, 2, 3, 4))
                    x, y = (13, 12) if package else (2, 3)
                    self.assertLess(obstacle.distance(x, y), 0)
                    grid = Grid(root, 'N', .2, .1, .5, (0, 0), layers=(1, 2, 15, 16),
                                window=(0, 0, 18, 18))
                    for layer in grid.layers:
                        self.assertEqual(_seg_clear(grid, layer, (x-2, y), (x+2, y)), layer in (2, 15))
                    self.assertTrue(grid.via_blocked[grid.ij(x, y)])
                    self.assertEqual(ET.tostring(root), before)

    def test_combined_mask_does_not_admit_unknown_masks_or_geometry(self):
        invalid = [native(f'restrictlayers="{mask}"') for mask in
                   ('1,2,16', '1,16,64', '1,1', '1,,16', '1 16', '1, 16', '0', '65535')]
        combined = native('restrictlayers="1,16"')
        invalid += [native('restrictlayers="1,16"', layer=43),
                    combined.replace('</polygonshape>', '<hole x="2" y="3"/></polygonshape>'),
                    combined.replace('x="3" y="2"', 'x="3" y="2" curve="90"'),
                    combined.replace('layer="41"', 'layer="41" unknown="yes"')]
        for shape in invalid:
            with self.subTest(shape=shape):
                with self.assertRaises(UnverifiedGeometry):
                    board_obstacles(board(shape))

    def test_inner_only_mask_blocks_a_crossing_and_a_through_via(self):
        for shape in (rect(41, 'restrictlayers="2"'), native('restrictlayers="2"'),
                      '<circle x="2" y="3" radius="1" width="0" layer="41" restrictlayers="2"/>'):
            with self.subTest(shape=shape):
                root = board(shape)
                root.find('.//param[@name="layerSetup"]').set('value', '(1*2*15*16)')
                obstacle, = board_obstacles(root)[0]
                self.assertEqual(obstacle.layers, {2})
                grid = Grid(root, 'N', .2, .1, .5, (0, 0), layers=(1, 16), window=(0, 0, 6, 6))
                for copper in (1, 16):
                    self.assertTrue(_seg_clear(grid, copper, (0, 3), (4, 3)))
                self.assertTrue(grid.via_blocked[grid.ij(2, 3)])
                grid = Grid(root, 'N', .2, .1, .5, (0, 0), layers=(1, 2, 15, 16), window=(0, 0, 6, 6))
                self.assertFalse(_seg_clear(grid, 2, (0, 3), (4, 3)))
                self.assertTrue(_seg_clear(grid, 15, (0, 3), (4, 3)))

    def test_invalid_shapes_fail_closed(self):
        invalid = [rect(extra='rot="R90"'), rect(extra='rot="MR0"'),
                   rect().replace('x2="3"', 'x2="1"'), rect().replace('x1="1"', 'x1="nan"'),
                   rect().replace('x1="1"', ''), native(''), native('restrictlayers="3"'),
                   native().replace('<vertex x="1" y="2"/></polygonoutlinesegments>', '</polygonoutlinesegments>'),
                   native().replace('</polygonshape>', '<hole x="2" y="3"/></polygonshape>'),
                   native().replace('x="3" y="2"', 'x="3" y="2" curve="90"')]
        for shape in invalid:
            with self.subTest(shape=shape):
                with self.assertRaises(UnverifiedGeometry):
                    board_obstacles(board(shape))

    def test_nonrestrict_ignored_and_zero_rotation(self):
        self.assertEqual(board_obstacles(board(rect(21)))[0], [])
        self.assertEqual(board_obstacles(board(native(layer=21)))[0], [])
        self.assertEqual(len(board_obstacles(board(rect(extra='rot="R0"')))[0]), 1)

    def test_mask_uses_enabled_export_ids_and_mirrors_stack_positions(self):
        root = board(native('restrictlayers="2,16"'), package=True)
        root.find('.//param[@name="layerSetup"]').set('value', '(1*2*16*64)')
        obstacle, = board_obstacles(root)[0]
        self.assertEqual(obstacle.layers, {2, 16})
        root.find('.//polygonshape').set('restrictlayers', '1,2')
        obstacle, = board_obstacles(root)[0]
        self.assertEqual(obstacle.layers, {16, 64})

    def test_coincident_native_outer_and_via_rectangles_preserve_each_restriction(self):
        trace = '<rectangle x1="90.04" y1="0" x2="97.96" y2="107" layer="41" restrictlayers="1,16"/>'
        via = '<rectangle x1="90.04" y1="0" x2="97.96" y2="107" layer="43"/>'
        for shapes, masks in ((trace + via, {41: {1, 16}, 43: set()}), (via, {43: set()})):
            with self.subTest(masks=masks):
                root = board(shapes)
                root.find('.//param[@name="layerSetup"]').set('value', '(1*2*15*16)')
                original = ET.tostring(root)
                obstacles, _, _ = board_obstacles(root)
                self.assertEqual(len(obstacles), len(masks))
                self.assertEqual({o.restrict_layer: o.layers for o in obstacles}, masks)
                self.assertEqual({o.bounds for o in obstacles}, {(90.04, 0, 97.96, 107)})
                grid = Grid(root, 'N', .2, .1, .5, (94, 53.5), layers=(1, 2, 15, 16),
                            window=(84, 49, 100, 58))
                for layer in grid.layers:
                    self.assertEqual(_seg_clear(grid, layer, (88, 53.5), (99, 53.5)),
                                     layer not in masks.get(41, ()))
                self.assertTrue(grid.via_blocked[grid.ij(94, 53.5)])
                self.assertFalse(grid.via_blocked[grid.ij(88, 53.5)])
                self.assertEqual(ET.tostring(root), original)
