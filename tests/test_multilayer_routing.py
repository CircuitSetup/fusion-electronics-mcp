import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from fusion_offline import design as D, router as R, route_all as RA
from fusion_mcp import server as S
from fusion_mcp.session import Snapshot
from test_offline import RouterTest


def board():
    root = ET.fromstring(RouterTest.BOARD)
    ET.SubElement(root.find('.//designrules'), 'param', name='layerSetup', value='(1*2*15*16)')
    return root


def fusion_export(setup='(1+2*63+64)'):
    """Legacy export layer table with Fusion's unchanged 64-ID design-rule setup."""
    root = board()
    root.find('.//param[@name="layerSetup"]').set('value', setup)
    layers = ET.SubElement(root.find('drawing'), 'layers')
    for number in range(1, 17):
        ET.SubElement(layers, 'layer', number=str(number),
                      name={1: 'Top', 16: 'Bottom'}.get(number, f'Route{number}'),
                      active='yes' if number in (1, 2, 16) else 'no')
    for element in root.findall('.//elements/element'):
        element.set('rot', 'MR0')
    root.find('.//signal[@name="X"]/wire').set('layer', '16')
    return root


class MultilayerTest(unittest.TestCase):
    def test_fusion_setup_uses_declared_export_ids_without_mutating_xml(self):
        for setup, expected in [('(1*64)', [1, 16]),
                ('(1+2*63+64)', [1, 2, 15, 16]),
                ('(1+2+3*62+63+64)', [1, 2, 3, 14, 15, 16]),
                ('(1+2+3+4*61+62+63+64)', [1, 2, 3, 4, 13, 14, 15, 16])]:
            with self.subTest(setup=setup):
                root = fusion_export(setup)
                before = ET.tostring(root)
                parsed = D.parse_board_design(root)
                self.assertEqual(parsed.copper_layers, expected)
                self.assertEqual(parsed.rules['layerSetup'], setup)
                self.assertEqual(ET.tostring(root), before)

    def test_declared_64_and_native_copper_ids_are_preserved(self):
        for numbers in [(1, 2, 63, 64), (1, 2, 303, 304)]:
            with self.subTest(numbers=numbers):
                root = board()
                root.find('.//param[@name="layerSetup"]').set('value', '('+'*'.join(map(str, numbers))+')')
                layers = ET.SubElement(root.find('drawing'), 'layers')
                for number in numbers:
                    ET.SubElement(layers, 'layer', number=str(number),
                                  name='Top' if number == 1 else 'Bottom' if number == numbers[-1] else f'Route{number}')
                self.assertEqual(D.parse_board_design(root).copper_layers, list(numbers))

    def test_ambiguous_or_unrepresentable_setup_is_rejected(self):
        for kind in ('missing_target', 'unrepresentable', 'conflicting_id', 'unknown_dialect'):
            with self.subTest(kind=kind):
                root = fusion_export()
                layers = root.find('./drawing/layers')
                if kind == 'missing_target':
                    layers.remove(layers.find("layer[@number='15']"))
                elif kind == 'unrepresentable':
                    root.find('.//param[@name="layerSetup"]').set('value', '(1+9*63+64)')
                elif kind == 'conflicting_id':
                    ET.SubElement(layers, 'layer', number='63', name='Other')
                else:
                    layers.find("layer[@number='16']").set('name', 'Other')
                with self.assertRaisesRegex(ValueError, '[Ll]ayer'):
                    D.parse_board_design(root).copper_layers

    def test_bottom_pad_and_retained_copper_share_export_layer(self):
        root = fusion_export()
        target = R.pad_target(root, 'A', '1')
        self.assertEqual(target[2], (16,))
        ET.SubElement(root.find('.//signal[@name="S"]'), 'wire',
                      x1='5.03', y1='10.07', x2='10', y2='10.07', width='.25', layer='16')
        self.assertEqual(R._net_traces(root, 'S'), [(16, (5.03, 10.07), (10., 10.07))])
        fragment, _ = R.fragment(root, 'S', target[:2], start_layers=target[2], include_layers=True)
        self.assertEqual(fragment, {(16, (5.03, 10.07), (10., 10.07))})

    def test_public_bottom_route_avoids_retained_bottom_copper(self):
        root = fusion_export()
        snap = Snapshot(board_xml=ET.tostring(root))
        with patch.object(S, '_planning_snapshot', return_value=(snap, None, {})), \
                patch.object(S.session, 'context', return_value={}), \
                patch('fusion_offline.render.render', return_value={}):
            result = S.route_trace.__wrapped__('A.1', 'B.1', width_mm=.25, layer='bottom',
                                             allow_vias=False, grid_mm=.254)
        self.assertFalse(result['route']['problems'])
        self.assertEqual({leg['layer'] for leg in result['route']['legs']}, {16})
        self.assertGreater(result['route']['length_mm'], 31)
        self.assertFalse(result['written'])
        self.assertEqual(ET.tostring(root), snap.board_xml)

    def test_mixed_export_maps_to_native_stack_by_order(self):
        root = fusion_export()
        stack = '<layerstackup>' + ''.join(f'<layerdef type="Signal" layer="{l}"/>' for l in [1,2,303,304]) + '</layerstackup>'
        with patch.object(S.session.bridge, 'call', return_value={'xml': stack}):
            self.assertEqual(S._routing_layer_map(root), {1:1, 2:2, 15:303, 16:304})
            self.assertEqual(S._read_layer(Snapshot(board_xml=ET.tostring(root)).board(), 304), 16)

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
