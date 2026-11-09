import contextlib
import copy
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from fusion_mcp import server as S
from fusion_mcp.session import Snapshot, WriteFailed


POINTS = [[8., 8.], [14., 8.], [14., 12.], [8., 12.]]


def pour():
    return dict(net='GND', layer=304, thermals=True, thermal_width_mm=.3,
                isolate_mm=.25, width_mm=.25, rank=1, orphans=False,
                hatched=False, outline=copy.deepcopy(POINTS))


def snapshot(pours, layers=(1, 304)):
    root = ET.fromstring('<eagle><drawing><board><signals/></board></drawing></eagle>')
    rules = ET.SubElement(root.find('.//board'), 'designrules')
    ET.SubElement(rules, 'param', name='layerSetup', value='(' + '*'.join(
        str({302: 14, 303: 15, 304: 16}.get(n, n)) for n in layers) + ')')
    signals = root.find('.//signals')
    for row in pours:
        signal = signals.find(f"signal[@name='{row['net']}']")
        if signal is None:
            signal = ET.SubElement(signals, 'signal', name=row['net'])
        polygon = ET.SubElement(signal, 'polygon', layer=str({302: 14, 303: 15, 304: 16}.get(row['layer'], row['layer'])), width=str(row['width_mm']))
        for x, y in row['outline']:
            ET.SubElement(polygon, 'vertex', x=str(x), y=str(y))
    return Snapshot(board_xml=ET.tostring(root))


class FakeSession:
    def __init__(self, before, after):
        self.before, self.after = before, after
        self.last_requirements = None
        self.commands = []
        self.bridge = self
        self.lock = self
        self.layers = [1, 304]
        self.inventory = None
        self.physical_names = None

    def hold(self, label):
        return contextlib.nullcontext()

    def snapshot(self, board=True, schematic=True):
        return snapshot(self.after if self.commands else self.before, self.layers)

    def call(self, op, *args, **kwargs):
        if op == 'layers':
            if self.inventory is not None:
                return {'layers': copy.deepcopy(self.inventory)}
            return {'layers': [dict(number=n, name='Top' if i == 0 else 'Bottom' if i == len(self.layers)-1
                                   else f'Inner {i}', visible=True) for i, n in enumerate(self.layers)]}
        if op == 'design_rules':
            return {'xml': '<layerstackup>' + ''.join(
                f'<layerdef type="Signal" layer="{n}" name="{self.physical_names[i] if self.physical_names else ""}"/>'
                for i, n in enumerate(self.layers)) + '</layerstackup>'}
        assert op == 'pours', op
        return {'pours': copy.deepcopy(self.after if self.commands else self.before)}

    def verified_write(self, editor, commands, verify, **kwargs):
        self.commands.append(commands)
        ok, detail = verify(snapshot(self.before, self.layers), snapshot(self.after, self.layers))
        if not ok:
            raise WriteFailed(detail)
        return None, detail


class PourCreationTest(unittest.TestCase):
    def classic_session(self, after, before=None):
        from test_cli_layers import CLASSIC_INVENTORY
        fake = FakeSession(before or [], after)
        fake.layers = [1, 2, 303, 304]
        fake.inventory = copy.deepcopy(CLASSIC_INVENTORY)
        next(row for row in fake.inventory if row['number'] == 15)['name'] = 'L3'
        fake.physical_names = ['Top', 'Route2', 'L3', 'Bottom']
        return fake

    def test_classic_api_layers_match_physical_inner_and_bottom_requests(self):
        for requested, api_layer, selector in [('inner2', 15, 'L3'), (303, 15, 'L3'),
                                               (15, 15, 'L3'), ('bottom', 16, 'Bottom'),
                                               (304, 16, 'Bottom'), (16, 16, 'Bottom')]:
            actual = pour(); actual['layer'] = api_layer
            fake = self.classic_session([actual])
            with self.subTest(requested=requested), patch.object(S, 'session', fake):
                result = S.add_pour.__wrapped__('GND', requested, points_mm=POINTS)
                self.assertTrue(result['ok'])
                self.assertIn(f"LAYER '{selector}';", fake.commands[0])

    def test_placeholder_inventory_layer_cannot_substitute_for_physical_stack_name(self):
        actual = pour(); actual['layer'] = 15
        fake = self.classic_session([actual])
        next(row for row in fake.inventory if row['number'] == 15)['name'] = 'Route15'
        with patch.object(S, 'session', fake):
            with self.assertRaisesRegex(ValueError, 'L3.*inventory'):
                S.add_pour.__wrapped__('GND', 'inner2', points_mm=POINTS)
        self.assertFalse(fake.commands)

    def test_missing_physical_name_cannot_fall_back_to_placeholder_classic_id(self):
        actual = pour(); actual['layer'] = 15
        fake = self.classic_session([actual])
        fake.physical_names = None
        with patch.object(S, 'session', fake):
            with self.assertRaisesRegex(ValueError, 'physical.*name'):
                S.add_pour.__wrapped__('GND', 'inner2', points_mm=POINTS)
        self.assertFalse(fake.commands)

    def test_physical_name_cannot_bind_to_a_different_inventory_position(self):
        for wrong_id in (1, 2, 3, 16, 41):
            actual = pour(); actual['layer'] = wrong_id
            fake = self.classic_session([actual])
            next(row for row in fake.inventory if row['number'] == 15)['name'] = 'Route15'
            wrong = next((row for row in fake.inventory if row['number'] == wrong_id), None)
            if wrong is None:
                fake.inventory.append(dict(number=wrong_id, name='L3', visible=False, used=False))
            else:
                wrong['name'] = 'L3'
            with self.subTest(wrong_id=wrong_id), patch.object(S, 'session', fake):
                with self.assertRaisesRegex(ValueError, 'L3.*inventory'):
                    S.add_pour.__wrapped__('GND', 'inner2', points_mm=POINTS)
            self.assertFalse(fake.commands)

    def test_physical_name_cannot_override_an_ambiguous_inventory_namespace(self):
        actual = pour(); actual['layer'] = 15
        fake = self.classic_session([actual])
        fake.inventory.extend([dict(number=303, name='Physical third', visible=False),
                               dict(number=304, name='Physical bottom', visible=False)])
        with patch.object(S, 'session', fake):
            with self.assertRaisesRegex(ValueError, 'Ambiguous.*namespace'):
                S.add_pour.__wrapped__('GND', 'inner2', points_mm=POINTS)
        self.assertFalse(fake.commands)

    def test_classic_api_rejects_wrong_layer_and_physical_namespace_readback(self):
        for api_layer in (1, 2, 16, 303, 304, 999):
            actual = pour(); actual['layer'] = api_layer
            fake = self.classic_session([actual])
            with self.subTest(api_layer=api_layer), patch.object(S, 'session', fake):
                with self.assertRaisesRegex(WriteFailed, 'read-back mismatch: layer'):
                    S.add_pour.__wrapped__('GND', 'inner2', points_mm=POINTS)

    def test_thermal_filter_uses_classic_api_layer_and_restores_view(self):
        other = pour(); other['layer'] = 2
        target = pour(); target['layer'] = 15
        changed = copy.deepcopy(target); changed['thermal_width_mm'] = .35
        fake = self.classic_session([other, changed], [other, target])
        fake.run = lambda command, editor: fake.commands.append(command)
        with patch.object(S, 'session', fake), patch.object(S._only_layers, 'last_view', None, create=True):
            result = S.set_pour_thermals.__wrapped__('GND', .35, layer='inner2')
        self.assertEqual(result['pours'], [{'layer': 15, 'thermal_width_mm': .35}])
        self.assertIn("DISPLAY NONE 'L3'; CHANGE THERMALWIDTH 0.35", fake.commands[0])
        self.assertIn("DISPLAY NONE 'Top' 'Route2' 'L3' 'Bottom'", fake.commands[0])

    def test_thermal_pick_does_not_reinterpret_inventory_id_as_physical_id(self):
        target = pour(); target['layer'] = 16
        changed = copy.deepcopy(target); changed['thermal_width_mm'] = .35
        fake = self.classic_session([changed], [target])
        fake.layers = [1, 16, 303, 304]
        exported = snapshot([target], [1, 2, 303, 304])
        fake.run = lambda command, editor: fake.commands.append(command)
        with patch.object(S, 'session', fake), patch.object(S, '_snap', return_value=exported), \
                patch.object(S._only_layers, 'last_view', None, create=True):
            result = S.set_pour_thermals.__wrapped__('GND', .35, layer='bottom')
        self.assertEqual(result['pours'], [{'layer': 16, 'thermal_width_mm': .35}])
        self.assertIn("DISPLAY NONE 'Bottom'; CHANGE THERMALWIDTH 0.35", fake.commands[0])

    def test_configured_layers_and_independent_same_layer_priorities(self):
        for native, exported in [([1, 304], [1, 16]),
                                 ([1, 2, 303, 304], [1, 2, 15, 16]),
                                 ([1, 2, 3, 302, 303, 304], [1, 2, 3, 14, 15, 16])]:
            names = ['top', *[f'inner{i}' for i in range(1, len(native)-1)], 'bottom']
            root = ET.fromstring('<eagle><drawing><board><designrules/><signals/></board></drawing></eagle>')
            ET.SubElement(root.find('.//designrules'), 'param', name='layerSetup',
                          value='(' + '*'.join(map(str, exported)) + ')')
            snap = Snapshot(board_xml=ET.tostring(root))
            for alias, number, export in zip(names, native, exported):
                for requested in (alias, number, export):
                    for rank in range(1, 7):
                        background = pour(); background.update(net='VOUT', layer=number, rank=6)
                        other_layer = pour(); other_layer['layer'] = native[-1] if number == native[0] else native[0]
                        actual = pour(); actual.update(layer=number, rank=rank)
                        fake = FakeSession([background, other_layer], [other_layer, actual, background])
                        fake.layers = native
                        with self.subTest(layers=native, requested=requested, rank=rank), \
                                patch.object(S, 'session', fake), patch.object(S, '_snap', return_value=snap):
                            result = S.add_pour('GND', requested, points_mm=POINTS, rank=rank)
                            self.assertTrue(result['ok'])
                            selector = "'Top'" if number == native[0] else "'Bottom'" if number == native[-1] else f"'Inner {native.index(number)}'"
                            self.assertIn(f'LAYER {selector};', fake.commands[0])
                            self.assertIn(f'CHANGE RANK {rank};', fake.commands[0])

    def test_unconfigured_inner_and_noncopper_layers_refused_before_write(self):
        fake = FakeSession([], []); fake.layers = [1, 304]
        snap = snapshot([])
        for layer in ('inner1', 'inner0', 21):
            with self.subTest(layer=layer), patch.object(S, 'session', fake), \
                    patch.object(S, '_snap', return_value=snap):
                with self.assertRaisesRegex(Exception, 'layer'):
                    S.add_pour('GND', layer, points_mm=POINTS)
                self.assertFalse(fake.commands)

    def test_rank_must_be_an_integer_in_range_before_write(self):
        for rank in (0, 7, -1, 1.5, True, '2'):
            actual = pour(); actual['rank'] = int(rank)
            fake = FakeSession([], [actual])
            with self.subTest(rank=rank), patch.object(S, 'session', fake):
                with self.assertRaisesRegex(Exception, 'rank'):
                    S.add_pour('GND', points_mm=POINTS, rank=rank)
                self.assertFalse(fake.commands)

    def run_pour(self, after, before=None, **kwargs):
        fake = FakeSession(before or [], after)
        with patch.object(S, 'session', fake):
            result = S.add_pour('GND', 'bottom', points_mm=POINTS, **kwargs)
        return fake, result

    def test_direct_connection_and_live_native_outline(self):
        actual = pour(); actual['thermals'] = False
        fake, result = self.run_pour([actual], thermals=False)
        self.assertIn('CHANGE THERMALS OFF;', fake.commands[0])
        self.assertTrue(result['ok'])
        self.assertEqual(result['verification']['filled_copper'], 'unverified')

    def test_default_relief_and_cyclic_reversed_outline(self):
        actual = pour(); actual['outline'] = list(reversed(POINTS[2:] + POINTS[:2]))
        fake, _ = self.run_pour([actual])
        self.assertIn('CHANGE THERMALS ON;', fake.commands[0])

    def test_wrong_geometry_or_settings_cannot_pass_by_count(self):
        for field, value in [('net', 'VOUT'), ('layer', 1), ('thermals', False),
                             ('thermal_width_mm', .1), ('isolate_mm', .1), ('width_mm', .1),
                             ('rank', 2), ('orphans', True), ('hatched', True),
                             ('outline', [[9, 8], *POINTS[1:]])]:
            actual = pour(); actual[field] = value
            with self.subTest(field=field), self.assertRaises(Exception):
                self.run_pour([actual])

    def test_existing_pour_must_be_preserved(self):
        old = pour(); old['net'] = 'VOUT'
        changed = copy.deepcopy(old); changed['isolate_mm'] = .1
        with self.assertRaisesRegex(Exception, 'existing pour'):
            self.run_pour([changed, pour()], before=[old])

    def test_invalid_outline_refused_before_write(self):
        for points in [[], [[0, 0], [1, 1]], [[0, 0], [1, 1], [2, 2]],
                       [[0, 0], [float('nan'), 1], [2, 0]]]:
            fake = FakeSession([], [])
            with self.subTest(points=points), patch.object(S, 'session', fake):
                with self.assertRaises(Exception):
                    S.add_pour('GND', points_mm=points)
                self.assertFalse(fake.commands)


if __name__ == '__main__':
    unittest.main()
