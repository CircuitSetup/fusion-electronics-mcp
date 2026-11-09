import copy
import hashlib
import importlib
import importlib.util
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import numpy as np

from fusion_mcp import server
from fusion_mcp.session import Session, Snapshot, WriteFailed
from fusion_offline import router, stitch
from test_package_restrict import board, grid
from test_move_parts import FakeFusion


def digest(element):
    return hashlib.sha256(ET.canonicalize(ET.tostring(element, encoding='unicode'), strip_text=True).encode()).hexdigest()


def declaration(root):
    return {'source_ref': 'U1', 'package_sha256': digest(root.find('.//package[@name="RF"]')),
            'source_element_sha256': digest(root.find('.//element[@name="U1"]')),
            'header_refs': ['J2', 'J3'], 'protected_region_mm': [8, 12, 12, 20], 'layers': [1],
            'connections': [{'pads': ['J2.1', 'J3.1'], 'corridor_mm': [1, 9, 19, 11]}],
            'reason': 'Synthetic explicit header-only exception; no native DRC waiver.'}


class HeaderKeepoutExceptionTest(unittest.TestCase):
    def scoped(self, root, spec, from_pad='J2.1', to_pad='J3.1'):
        self.assertIsNotNone(importlib.util.find_spec('fusion_offline.keepouts'), 'Header corridor support is missing')
        module = importlib.import_module('fusion_offline.keepouts')
        return module.header_connection_obstacles(root, spec, from_pad, to_pad)

    def test_only_declared_connection_gets_a_trace_corridor(self):
        root = board()
        before = ET.tostring(root)
        cache, scope = self.scoped(root, declaration(root))
        route = router.route(root, 'S', (2, 10), (1,), (18, 10), (1,), layers=(1,),
                             vias=False, step=.25, cache=cache)
        self.assertFalse(route.problems)
        self.assertEqual(scope['connection'], ['J2.1', 'J3.1'])
        self.assertFalse(scope['native_drc_waived'])
        self.assertTrue(scope['native_keepout_unchanged'])
        self.assertEqual(ET.tostring(root), before)
        strict = grid(root)
        self.assertFalse(strict.free(*strict.ij(10, 10), 1))

    def test_reverse_connection_is_the_same_declared_exception(self):
        root = board()
        cache, scope = self.scoped(root, declaration(root), 'J3.1', 'J2.1')
        g = router.Grid(root, 'S', .2, .1, .25, (0, 0), cache=cache)
        self.assertTrue(g.free(*g.ij(10, 10), 1))

    def test_vias_and_central_exclusion_remain_blocked(self):
        root = board()
        cache, _ = self.scoped(root, declaration(root))
        g = router.Grid(root, 'S', .2, .1, .25, (0, 0), layers=(1, 2, 15, 16), cache=cache)
        self.assertTrue(g.free(*g.ij(10, 10), 1))
        self.assertTrue(g.via_blocked[g.ij(10, 10)])
        self.assertTrue(all(not g.free(*g.ij(10, 15), layer) for layer in g.layers))
        self.assertTrue(g.via_blocked[g.ij(10, 15)])
        route = router.Route('S', legs=[(1, [(2, 10), (18, 10)])], vias=[(10, 10)])
        self.assertTrue(router.check_route(g, route))

    def test_corridor_edge_keeps_full_trace_width_and_clearance(self):
        root = board()
        cache, _ = self.scoped(root, declaration(root))
        g = router.Grid(root, 'S', .4, .1, .05, (0, 0), cache=cache)
        self.assertFalse(g.free(*g.ij(10, 9.1), 1))
        self.assertTrue(g.free(*g.ij(10, 9.4), 1))
        obstacles = [o for o in cache[0] if o.restrict_layer == 41]
        xs, ys = np.array([10., 10., 10.]), np.array([8.5, 9.1, 10.])
        for obstacle in obstacles:
            np.testing.assert_allclose(router._dist_field(obstacle, xs, ys), [obstacle.distance(x, y) for x, y in zip(xs, ys)])

    def test_other_side_package_and_unconnected_copper_remain_blocked(self):
        root = board()
        package = root.find('.//package[@name="RF"]')
        bottom = copy.deepcopy(package.find('polygon')); bottom.set('layer', '42'); package.append(bottom)
        other = ET.SubElement(root.find('.//elements'), 'element', name='U2', library='L', package='RF', x='10', y='10')
        cache, _ = self.scoped(root, declaration(root))
        g = router.Grid(root, 'S', .2, .1, .25, (0, 0), cache=cache)
        self.assertFalse(g.free(*g.ij(10, 10), 1))
        self.assertFalse(g.free(*g.ij(10, 10), 16))
        self.assertEqual(len([o for o in cache[0] if o.source_ref == other.get('name')]), 2)

    def test_wrong_identity_reference_pair_and_missing_authority_reject(self):
        for change in ('package', 'pose', 'source_ref', 'header', 'pair', 'net', 'reason', 'unknown', 'layer'):
            with self.subTest(change=change):
                root = board(); spec = declaration(root)
                if change == 'package': root.find('.//package[@name="RF"]').set('name', 'CHANGED')
                if change == 'pose': root.find('.//element[@name="U1"]').set('x', '10.1')
                if change == 'source_ref': spec['source_ref'] = 'U99'
                if change == 'header': spec['header_refs'] = ['J4']
                if change == 'pair': spec['connections'][0]['pads'] = ['J2.1', 'J3.2']
                if change == 'net': root.find('.//signal/contactref').set('pad', '2')
                if change == 'reason': spec['reason'] = ''
                if change == 'unknown': spec['allowed_nets'] = ['S']
                if change == 'layer': spec['layers'] = [2]
                with self.assertRaises(ValueError): self.scoped(root, spec)

    def test_overlapping_protected_core_and_malformed_corridors_reject(self):
        for change in ('core', 'touching', 'nan', 'inverted', 'miss_header', 'missing_core', 'duplicate_pair'):
            with self.subTest(change=change):
                root = board(); spec = declaration(root)
                corridor = spec['connections'][0]
                if change == 'core': corridor['corridor_mm'] = [1, 9, 19, 13]
                if change == 'touching': corridor['corridor_mm'] = [1, 9, 19, 12]
                if change == 'nan': corridor['corridor_mm'][0] = float('nan')
                if change == 'inverted': corridor['corridor_mm'] = [19, 9, 1, 11]
                if change == 'miss_header': corridor['corridor_mm'] = [8, 9, 12, 11]
                if change == 'missing_core': spec['protected_region_mm'] = [0, 0, 1, 1]
                if change == 'duplicate_pair': spec['connections'].append(copy.deepcopy(corridor))
                with self.assertRaises(ValueError): self.scoped(root, spec)

    def test_protected_rectangle_cannot_span_a_concave_notch(self):
        root = board(points=((-2, -11), (2, -11), (2, 11), (.5, 11), (.5, 3), (-.5, 3), (-.5, 11), (-2, 11)))
        spec = declaration(root)
        spec['protected_region_mm'] = [8.5, 12, 11.5, 20]
        with self.assertRaises(ValueError): self.scoped(root, spec)

    def test_other_pad_pair_on_same_net_does_not_reuse_exception(self):
        root = board()
        ET.SubElement(root.find('.//elements'), 'element', name='J4', library='L', package='PIN', x='2', y='5')
        ET.SubElement(root.find('.//signal[@name="S"]'), 'contactref', element='J4', pad='1')
        with self.assertRaises(ValueError): self.scoped(root, declaration(root), 'J4.1', 'J3.1')

    def test_scoped_cache_cannot_relax_another_net_or_connection(self):
        root = board()
        cache, _ = self.scoped(root, declaration(root))
        g = router.Grid(root, 'OTHER', .2, .1, .25, (0, 0), cache=cache)
        self.assertFalse(g.free(*g.ij(10, 10), 1))
        route = router.route(root, 'S', (2, 10), (1,), (18, 9.5), (1,), layers=(1,),
                             vias=False, step=.25, max_nodes=10000, cache=cache)
        self.assertTrue(route.problems)

    def test_route_trace_pins_exception_write_and_does_not_mutate_snapshot(self):
        root = board(); raw = ET.tostring(root)
        contexts = {'active_document': {'document_id': 'board:1', 'kind': 'board', 'name': 'TEST'}}
        bridge = FakeFusion(root, design='TEST')
        bridge_context = bridge.call
        stack = '<layerstackup>' + ''.join(f'<layerdef type="Signal" layer="{layer}"/>' for layer in (1, 2, 15, 16)) + '</layerstackup>'
        def call(op, *args, **kwargs):
            if op == 'context': return contexts
            if op == 'design_rules':
                changed = copy.deepcopy(root)
                changed.find('.//element[@name="J2"]').set('x', '2.1')
                bridge.xml = ET.tostring(changed)
                return {'xml': stack}
            return bridge_context(op, *args, **kwargs)
        bridge.call = call
        session = Session(bridge)
        with mock.patch.object(server, 'session', session), \
                mock.patch.object(server, '_width_for', return_value=(.2, 'test')), \
                mock.patch.object(server, '_route_layer', return_value=1):
            with self.assertRaisesRegex(Exception, 'planned board changed'):
                server.route_trace('J2.1', 'J3.1', width_mm=.2, layer='top', allow_vias=False,
                                   grid_mm=.25, dry_run=False, keepout_exception=declaration(root))
        self.assertEqual(bridge.ran, [])

    def test_exception_planning_requires_active_board_identity(self):
        from fusion_mcp import design_requirements
        fake = mock.Mock()
        fake.context.return_value = {'active_document': {'document_id': 'sch:1', 'kind': 'schematic'}}
        with mock.patch.object(server, 'session', fake), \
                mock.patch.object(design_requirements, 'bound', return_value=None), \
                mock.patch.object(server, '_snap', return_value=Snapshot(board_xml=ET.tostring(board()))):
            with self.assertRaises(WriteFailed):
                server._planning_snapshot(pin_document=True)
