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

    def test_observed_outer_mask_cannot_be_assumed_to_describe_an_inner_layer(self):
        root = board(native('restrictlayers="16"'))
        root.find('.//param[@name="layerSetup"]').set('value', '(1*2*16*64)')
        with self.assertRaises(UnverifiedGeometry):
            board_obstacles(root)
