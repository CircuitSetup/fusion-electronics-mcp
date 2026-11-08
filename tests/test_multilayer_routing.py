import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from fusion_offline import router as R, route_all as RA
from fusion_mcp import server as S
from fusion_mcp.session import Snapshot
from test_offline import RouterTest


def board():
    root = ET.fromstring(RouterTest.BOARD)
    ET.SubElement(root.find('.//designrules'), 'param', name='layerSetup', value='(1*2*15*16)')
    return root


class MultilayerTest(unittest.TestCase):
    def test_fusion_through_via_command_uses_command_layer_numbers(self):
        route = R.Route('S', vias=[(10, 5)], via_extent=(1, 16))
        for native_bottom, command_bottom in [(304, 64), (16, 16), (64, 64)]:
            with self.subTest(bottom=native_bottom), patch.object(S, '_write_layer',
                    side_effect=lambda name: 1 if name == 'top' else native_bottom):
                commands = S._route_vias('S', route)
                self.assertIn(f'round 1-{command_bottom} (10 5)', commands[0])

    def test_route_on_inner_layer_and_preserve_commit(self):
        root = board()
        route = R.route(root, 'S', (5.03, 10.07), (2,), (35.11, 10.02), (2,), layers=(2,), vias=False, step=.254)
        self.assertFalse(route.problems)
        self.assertEqual({l for l, _ in route.legs}, {2})
        RA._commit(root, RA.Conn('inner', 'S', (), (), (), ()), route)
        self.assertIn(2, {l for l, _, _ in R._net_traces(root, 'S')})

    def test_inner_obstacles_and_invalid_layer(self):
        root = board()
        root.find('.//signal[@name="X"]/wire').set('layer', '2')
        route = R.route(root, 'S', (5.03, 10.07), (2,), (35.11, 10.02), (2,), layers=(2,), vias=False, step=.254)
        self.assertFalse(route.problems)
        self.assertGreater(route.length_mm, 31)
        with self.assertRaisesRegex(ValueError, 'layer'):
            R.route(root, 'S', (5, 10), (3,), (35, 10), (3,), layers=(3,))

    def test_fragment_respects_via_span(self):
        root = board()
        sg = root.find('.//signal[@name="S"]')
        for layer, end in [(1, 10), (2, 15), (15, 20)]:
            ET.SubElement(sg, 'wire', x1='5', y1='5', x2=str(end), y2='4' if layer == 1 else '5', width='.25', layer=str(layer))
        ET.SubElement(sg, 'via', x='5', y='5', extent='1-2', drill='.3', diameter='.6')
        self.assertEqual(R.via_layers(root, sg.find('via')), (1, 2))
        # A trace endpoint on layer 15 is coincident but no via spans to it.
        frag, _ = R.fragment(root, 'S', (10, 4))
        self.assertNotIn(((5., 5.), (20., 5.)), frag)

    def test_through_via_span_and_native_mapping(self):
        root = board()
        r = R.route(root, 'S', (5.03, 10.07), (1,), (35.11, 10.02), (15,), step=.254)
        self.assertFalse(r.problems)
        self.assertTrue(r.vias)
        self.assertEqual(r.via_extent, (1, 16))
        stack = '<layerstackup>' + ''.join(f'<layerdef type="Signal" layer="{l}"/>' for l in [1,2,303,304]) + '</layerstackup>'
        snap = Snapshot(board_xml=ET.tostring(root))
        with patch.object(S.session.bridge, 'call', return_value={'xml': stack}):
            self.assertEqual(S._routing_layer_map(root), {1:1,2:2,15:303,16:304})
            self.assertEqual(S._read_layer(snap.board(), 303), 15)

    def test_stack_mismatch_refuses_mapping(self):
        with patch.object(S.session.bridge, 'call', return_value={'xml': '<rules/>'}):
            with self.assertRaisesRegex(ValueError, 'stack'):
                S._routing_layer_map(board())
            root = board()
            root.find('.//param[@name="layerSetup"]').set('value', '(1*16)')
            with self.assertRaisesRegex(ValueError, 'stack'):
                S._route_write_map(root)

    def test_two_layer_export_cannot_hide_native_inner_layers(self):
        root = board()
        root.find('.//param[@name="layerSetup"]').set('value', '(1*16)')
        stack = '<layerstackup>' + ''.join(f'<layerdef type="Signal" layer="{l}"/>' for l in [1,2,303,304]) + '</layerstackup>'
        with patch.object(S.session.bridge, 'call', return_value={'xml': stack}):
            with self.assertRaisesRegex(ValueError, 'stack'):
                S._route_write_map(root)
            with self.assertRaisesRegex(ValueError, 'stack'):
                S._read_layer(Snapshot(board_xml=ET.tostring(root)).board(), 304)

    def test_unsupported_via_span_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported via'):
            R.route(board(), 'S', (5,10), (1,), (35,10), (2,), via_spans=[[1,2]])

    def test_readback_verifies_native_inner_width_and_via_span(self):
        root = board()
        r = R.Route('S', legs=[(15, [(7,5),(10,5)])], vias=[(10,5)], via_extent=(1,16))
        sg = root.find('.//signal[@name="S"]')
        wire = ET.SubElement(sg, 'wire', x1='7', y1='5', x2='10', y2='5', width='.5', layer='15')
        via = ET.SubElement(sg, 'via', x='10', y='5', drill='.3', diameter='.6', extent='1-16')
        stack = '<layerstackup>' + ''.join(f'<layerdef type="Signal" layer="{l}"/>' for l in [1,2,303,304]) + '</layerstackup>'
        with patch.object(S.session.bridge, 'call', return_value={'xml':stack}):
            self.assertEqual(S._verify_route(Snapshot(board_xml=ET.tostring(root)), 'S', r, .5, {15:303}), [])
            wire.set('width','.25')
            self.assertTrue(S._verify_route(Snapshot(board_xml=ET.tostring(root)), 'S', r, .5, {15:303}))
            wire.set('width','.5'); via.set('extent','1-2')
            self.assertTrue(S._verify_route(Snapshot(board_xml=ET.tostring(root)), 'S', r, .5, {15:303}))

    def test_inner_preference_without_vias_cannot_route_outer_smds(self):
        snap = Snapshot(board_xml=ET.tostring(board()))
        with patch.object(S, '_planning_snapshot', return_value=(snap,None,{})), \
                patch.object(S.session, 'context', return_value={}):
            result = S.route_trace.__wrapped__('A.1','B.1',width_mm=.25,layer='inner1',allow_vias=False)
        self.assertTrue(result['route']['problems'])
