import copy
import re
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from fusion_mcp import commands as C, server as S
from fusion_mcp.session import Session, Snapshot, WriteFailed
from test_move_parts import FakeFusion
from test_multilayer_routing import fusion_export


CLASSIC_INVENTORY = [dict(number=n, name=name, visible=True, used=True) for n, name in
                     [(1, 'Top'), (2, 'Route2'), (15, 'Route15'), (16, 'Bottom'),
                      (18, 'Vias'), (41, 'RestrictCopper'), (42, 'bRestrict'), (43, 'vRestrict')]]


class CliFusion(FakeFusion):
    """V2 inventory IDs differ from both CLI names and exported geometry IDs."""
    def __init__(self, wrong_layer=False):
        super().__init__(fusion_export())
        self.native = [1, 2, 303, 304]
        self.exported = [1, 2, 15, 16]
        self.inventory = [dict(number=n, name=name, visible=True) for n, name in
                          [(1, 'Top'), (2, 'Route2'), (303, 'Inner return'), (304, 'Bottom'), (23, 'Origins')]]
        self.wrong_layer = wrong_layer

    def call(self, op, args=None, **kwargs):
        if op == 'design_rules':
            return {'xml': '<layerstackup>' + ''.join(
                f'<layerdef type="Signal" layer="{n}"/>' for n in self.native) + '</layerstackup>'}
        if op == 'layers':
            return {'layers': copy.deepcopy(self.inventory)}
        if op == 'run' and 'SET ' in args['commands'] and 'WIRE ' not in args['commands']:
            self.ran.append(args['commands'])
            return {'raw': '', 'dialogs': []}
        if op == 'run' and 'WIRE ' in args['commands']:
            commands = args['commands']
            self.ran.append(commands)
            self.undo.append(self.xml)
            root = ET.fromstring(self.xml)
            for selector, net, width, path in re.findall(
                    r"LAYER ([^;]+);.*?WIRE '([^']+)' ([\d.]+) ([^;]+);", commands):
                if selector.isdigit():
                    native = int(selector)
                    # The old invalid LAYER304 leaves the previous Top layer selected.
                    layer = self.exported[self.native.index(native)] if native in (1, 2) else 1
                else:
                    name = selector.strip("'")
                    native = next(row['number'] for row in self.inventory if row['name'] == name)
                    layer = self.exported[self.native.index(native)] if native in self.native else native
                if self.wrong_layer:
                    layer = 1
                signal = root.find(f'.//signal[@name="{net}"]')
                points = [(float(x), float(y)) for x, y in re.findall(r'\(([-\d.]+) ([-\d.]+)\)', path)]
                for a, b in zip(points, points[1:]):
                    ET.SubElement(signal, 'wire', x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]),
                                  width=width, layer=str(layer))
            self.xml = ET.tostring(root)
            return {'raw': '', 'dialogs': []}
        return super().call(op, args, **kwargs)


class CliLayerTest(unittest.TestCase):
    def test_physical_export_and_command_layer_maps_are_distinct(self):
        fake = CliFusion()
        root = ET.fromstring(fake.xml)
        with patch.object(S, 'session', Session(fake)), patch.object(S, '_snap', return_value=Snapshot(board_xml=fake.xml)):
            self.assertEqual(S._routing_layer_map(root), {1: 1, 2: 2, 15: 303, 16: 304})
            self.assertEqual(S._route_write_map(root), {1: 'Top', 2: 'Route2', 15: 'Inner return', 16: 'Bottom'})
            for requested in ('bottom', 16, 304):
                self.assertEqual(S._write_layer(requested), 'Bottom')
            self.assertEqual(S._write_layer('inner2'), 'Inner return')
            self.assertEqual(S._read_layer(Snapshot(board_xml=fake.xml).board(), 304), 16)
            self.assertEqual(S._read_layer(Snapshot(board_xml=fake.xml).board(), 303), 15)

    def test_classic_inventory_is_joined_to_v2_stack_through_export_ids(self):
        fake = CliFusion()
        fake.inventory = copy.deepcopy(CLASSIC_INVENTORY)
        with patch.object(S, 'session', Session(fake)):
            self.assertEqual(S._write_layer('bottom'), 'Bottom')
            self.assertEqual(S._write_layer('inner2'), 'Route15')
            self.assertEqual(S._route_write_map(ET.fromstring(fake.xml)),
                             {1: 'Top', 2: 'Route2', 15: 'Route15', 16: 'Bottom'})
            self.assertEqual(S._command_layer(41), 'RestrictCopper')
        self.assertTrue(self.route(fake)['written'])

    def test_bottom_only_write_does_not_resolve_unused_inner_layer_names(self):
        fake = CliFusion()
        fake.inventory = [row for row in copy.deepcopy(CLASSIC_INVENTORY) if row['number'] in (1, 16)]
        with patch.object(S, 'session', Session(fake)):
            self.assertEqual(S._route_write_map(ET.fromstring(fake.xml), used_layers={16}), {16: 'Bottom'})
        self.assertTrue(self.route(fake)['written'])

    def test_classic_inventory_does_not_confuse_overlapping_physical_inner_id(self):
        fake = CliFusion()
        fake.native = [1, 2, 3, 304]
        fake.inventory = copy.deepcopy(CLASSIC_INVENTORY) + [dict(number=3, name='Route3', visible=False, used=False)]
        with patch.object(S, 'session', Session(fake)):
            self.assertEqual(S._route_write_map(ET.fromstring(fake.xml), used_layers={15}), {15: 'Route15'})
            self.assertEqual(S._write_layer('inner2'), 'Route15')

    def test_trace_builder_quotes_semantic_names_and_rejects_v2_numbers(self):
        self.assertIn("LAYER 'Bottom';", C.add_trace('S', 'Bottom', .25, [(0, 0), (1, 1)]))
        self.assertIn("LAYER 'Inner return';", C.add_trace('S', 'Inner return', .25, [(0, 0), (1, 1)]))
        for layer in (0, 256, 303, 304, '304', '304 ', '-304', "Bottom'; WIRE 'X", 'Bottom\nWIRE'):
            with self.subTest(layer=layer), self.assertRaises(C.InvalidInput):
                C.add_trace('S', layer, .25, [(0, 0), (1, 1)])

    def test_via_builder_rejects_extended_api_extent_before_command_submission(self):
        with self.assertRaises(C.InvalidInput):
            C.add_via('S', 1, 2, .3, .6, extent=(1, 304))

    def route(self, fake):
        with patch.object(S, 'session', Session(fake)), patch('fusion_offline.render.render', return_value={}):
            return S.route_trace.__wrapped__('A.1', 'B.1', width_mm=.25, layer='bottom',
                                             allow_vias=False, grid_mm=.254, dry_run=False)

    def test_public_route_write_uses_bottom_name_and_reads_export16(self):
        fake = CliFusion()
        before = ET.fromstring(fake.xml).find('.//signal[@name="X"]')
        result = self.route(fake)
        self.assertTrue(result['written'])
        command = next(c for c in fake.ran if 'WIRE ' in c)
        self.assertIn("LAYER 'Bottom';", command)
        self.assertNotIn('LAYER 304', command)
        after = ET.fromstring(fake.xml)
        self.assertEqual({w.get('layer') for w in after.findall('.//signal[@name="S"]/wire')}, {'16'})
        self.assertEqual(ET.tostring(after.find('.//signal[@name="X"]')), ET.tostring(before))

    def test_wrong_layer_write_is_rejected_and_rolled_back(self):
        fake = CliFusion(wrong_layer=True)
        before = fake.xml
        with self.assertRaises(WriteFailed):
            self.route(fake)
        self.assertEqual(fake.xml, before)
        self.assertEqual(fake.ran[-1], 'UNDO;')

    def test_public_add_trace_uses_shared_command_mapping(self):
        fake = CliFusion()
        with patch.object(S, 'session', Session(fake)):
            result = S.add_trace.__wrapped__('S', 'bottom', .25, [[5.03, 10.07], [10, 10.07]])
        self.assertTrue(result['ok'])
        self.assertIn("LAYER 'Bottom';", fake.ran[0])
        self.assertEqual(ET.fromstring(fake.xml).find('.//signal[@name="S"]/wire').get('layer'), '16')

    def test_missing_ambiguous_or_unsafe_layer_inventory_refuses_mapping(self):
        for kind in ('missing', 'duplicate', 'unsafe'):
            fake = CliFusion()
            if kind == 'missing':
                fake.inventory = [row for row in fake.inventory if row['number'] != 304]
            elif kind == 'duplicate':
                fake.inventory.append(dict(number=200, name='BOTTOM', visible=False))
            else:
                fake.inventory[-2]['name'] = 'Bottom\nWIRE'
            with self.subTest(kind=kind), patch.object(S, 'session', Session(fake)):
                with self.assertRaises(ValueError):
                    S._route_write_map(ET.fromstring(fake.xml))
            self.assertEqual(fake.ran, [])

    def test_visibility_commands_resolve_native_ids_and_preserve_view(self):
        fake = CliFusion()
        with patch.object(S, 'session', Session(fake)):
            pre, post = S._only_layers([304])
        self.assertEqual(pre, "DISPLAY NONE 'Bottom';")
        self.assertIn("'Inner return'", post)
        self.assertIn("'Bottom'", post)
        self.assertNotIn('304', post)
        self.assertNotIn(' 23', post)

    def test_bus_write_plans_and_verifies_actual_non16_export_bottom(self):
        for exported in ([1, 2, 63, 64], [1, 2, 303, 304]):
            with self.subTest(exported=exported):
                fake = CliFusion()
                fake.exported = exported
                root = ET.fromstring(fake.xml)
                layers = root.find('./drawing/layers')
                layers.clear()
                for number in exported:
                    ET.SubElement(layers, 'layer', number=str(number),
                                  name='Top' if number == 1 else 'Bottom' if number == exported[-1] else f'Route{number}')
                root.find('.//param[@name="layerSetup"]').set('value', '('+'*'.join(map(str, exported))+')')
                root.find('.//signal[@name="X"]/wire').set('layer', str(exported[-1]))
                fake.xml = ET.tostring(root)
                with patch.object(S, 'session', Session(fake)), patch('fusion_offline.render.render', return_value={}):
                    result = S.lay_bus.__wrapped__(['S'], [[5, 19], [35, 19]], layer='bottom', dry_run=False)
                self.assertTrue(result['written'])
                copper = ET.fromstring(fake.xml).findall('.//signal[@name="S"]/wire')
                self.assertTrue(copper)
                self.assertEqual({w.get('layer') for w in copper}, {str(exported[-1])})
