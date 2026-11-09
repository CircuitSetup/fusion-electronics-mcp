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


def snapshot(pours):
    root = ET.fromstring('<eagle><drawing><board><signals/></board></drawing></eagle>')
    signals = root.find('.//signals')
    for row in pours:
        signal = signals.find(f"signal[@name='{row['net']}']")
        if signal is None:
            signal = ET.SubElement(signals, 'signal', name=row['net'])
        polygon = ET.SubElement(signal, 'polygon', layer=str(16 if row['layer'] == 304 else row['layer']), width=str(row['width_mm']))
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

    def hold(self, label):
        return contextlib.nullcontext()

    def call(self, op, *args, **kwargs):
        if op == 'layers':
            return {'layers': [dict(number=n, name='Top' if i == 0 else 'Bottom' if i == len(self.layers)-1
                                   else f'Inner {i}', visible=True) for i, n in enumerate(self.layers)]}
        if op == 'design_rules':
            return {'xml': '<layerstackup>' + ''.join(
                f'<layerdef type="Signal" layer="{n}"/>' for n in self.layers) + '</layerstackup>'}
        assert op == 'pours', op
        return {'pours': copy.deepcopy(self.after if self.commands else self.before)}

    def verified_write(self, editor, commands, verify, **kwargs):
        self.commands.append(commands)
        ok, detail = verify(snapshot(self.before), snapshot(self.after))
        if not ok:
            raise WriteFailed(detail)
        return None, detail


class PourCreationTest(unittest.TestCase):
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
                            selector = str(number) if number <= 255 else "'Bottom'" if number == native[-1] else f"'Inner {native.index(number)}'"
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
