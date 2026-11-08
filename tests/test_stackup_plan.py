import contextlib
import copy
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

from fusion_offline import stackup as ST
from fusion_mcp import server as S
from fusion_mcp.session import Snapshot


STACKS = Path(S.__file__).parent / 'data' / 'stackups'
FOUR = STACKS / 'JLC04161H-7628 4-layer.estackup'
ROLES = [{'role': 'signal', 'references': [2]}, {'role': 'ground', 'net': 'GND'},
         {'role': 'ground', 'net': 'GND'}, {'role': 'signal', 'references': [3]}]


def board(layers=(1, 16)):
    return Snapshot(board_xml=(f'<eagle><drawing><board><designrules><param name="layerSetup" '
        f'value="({"*".join(map(str, layers))})"/></designrules><signals/></board></drawing></eagle>').encode())


class StackupPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Mock()
        self.session.bridge.lock.hold.side_effect = lambda *a: contextlib.nullcontext()
        self.doc = {'kind': 'board', 'document_id': 'doc1', 'data_file_id': 'file1',
                    'name': 'Test', 'modified': False}
        self.session.context.side_effect = lambda: {'active_document': self.doc}
        self.session.bridge.call.return_value = {'xml': FOUR.read_text(), 'modified': 'now'}
        self.snap = board()
        self.addCleanup(patch.stopall)
        patch.object(S, 'session', self.session).start()
        patch.object(S, '_snap', side_effect=lambda **kw: self.snap).start()

    def prepare(self, **kw):
        args = dict(stackup_file=str(FOUR), layer_roles=ROLES,
                    rationale='Four layers needed for continuous reference planes and constrained routing.',
                    source_url='https://jlcpcb.com/impedance', out_dir=self.tmp.name)
        args.update(kw)
        return S.prepare_layer_stack(**args)

    def test_prepares_stack_only_without_replacing_rules_or_applying(self):
        out = self.prepare()
        self.assertEqual(out['status'], 'pending_import')
        root = ET.parse(out['file']).getroot()
        self.assertIsNotNone(root.find('layerstackup'))
        self.assertIsNone(root.find('designrules'))
        self.assertEqual(len(ST.parse_stackup(root).copper), 4)
        self.assertEqual(out['layer_roles'], ROLES)
        self.session.run.assert_not_called()
        self.session.verified_write.assert_not_called()
        self.assertTrue(Path(out['plan_file']).is_file())

    def test_two_four_six_and_eight_layer_physical_profiles(self):
        for count in (2, 4, 6):
            path = next(p for p in STACKS.glob('*.estackup') if len(ST.parse_stackup(p.read_bytes()).copper) == count)
            profile = ST.validate_stackup(path.read_bytes())
            self.assertEqual(len(profile['copper_layers']), count)
        root = ET.fromstring(FOUR.read_bytes()); stack = root.find('layerstackup')
        # A synthetic 8-layer test fixture, never a proposed fabrication stack.
        signal = copy.deepcopy(stack.find("layerdef[@type='Signal']"))
        dielectric = copy.deepcopy(stack.find("layerdef[@type='Prepreg']"))
        pos = list(stack).index(stack.find("layerdef[@type='Signal']")) + 1
        for num in (5, 4, 3, 2):
            s = copy.deepcopy(signal); s.set('layer', str(num + 10))
            stack.insert(pos, s); stack.insert(pos, copy.deepcopy(dielectric))
        for s, num in zip(stack.findall("layerdef[@type='Signal']"), [1, 2, 3, 4, 301, 302, 303, 304]):
            s.set('layer', str(num))
        self.assertEqual(len(ST.validate_stackup(root)['copper_layers']), 8)

    def test_invalid_physical_values_order_ids_and_vias_refused(self):
        for kind in ('zero', 'nan', 'missing_er', 'invalid_df', 'duplicate', 'no_dielectric', 'no_via', 'blind', 'micro'):
            root = ET.fromstring(FOUR.read_bytes()); stack = root.find('layerstackup')
            if kind in ('zero', 'nan'):
                stack.find("layerdef[@type='Signal']/material").set('thickness', '0mm' if kind == 'zero' else 'nan')
            elif kind == 'missing_er':
                stack.find("layerdef[@type='Prepreg']/material").attrib.pop('dielectric_constant_1g')
            elif kind == 'invalid_df':
                stack.find("layerdef[@type='Prepreg']/material").set('dissipation_factor', 'unknown')
            elif kind == 'duplicate':
                stack.findall("layerdef[@type='Signal']")[1].set('layer', '1')
            elif kind == 'no_dielectric':
                stack.remove(stack.find("layerdef[@type='Prepreg']"))
            elif kind == 'no_via':
                stack.remove(stack.find('viadef'))
            elif kind == 'micro':
                stack.find('viadef').set('microvia', 'yes')
            else:
                stack.find('viadef').set('stoplayer', '2')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                ST.validate_stackup(root)

    def test_roles_and_layer_removal_are_rejected(self):
        for roles in (ROLES[:2], [{'role': 'bogus'}] * 4,
                      [dict(ROLES[0], references=[3]), *ROLES[1:]],
                      [ROLES[0], {'role': 'ground'}, *ROLES[2:]]):
            with self.subTest(roles=roles), self.assertRaises(Exception):
                self.prepare(layer_roles=roles)
        self.snap = board((1, 2, 3, 14, 15, 16))
        with self.assertRaisesRegex(Exception, 'remov'):
            self.prepare()

    def test_check_requires_exact_physical_readback_and_export_count(self):
        out = self.prepare(); self.snap = board((1, 2, 15, 16))
        result = S.check_layer_stack(out['plan_file'])
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['layer_map'], {'1': 1, '2': 2, '15': 303, '16': 304})
        self.assertIn('reference-plane continuity', ' '.join(result['unverified']))
        for attr, value in [('thickness', '0.1mm'), ('dielectric_constant_1g', '3.9'), ('oem_material', 'other')]:
            root = ET.fromstring(FOUR.read_bytes())
            root.find(".//layerdef[@type='Prepreg']/material").set(attr, value)
            self.session.bridge.call.return_value['xml'] = ET.tostring(root).decode()
            with self.subTest(attr=attr):
                self.assertEqual(S.check_layer_stack(out['plan_file'])['status'], 'fail')
        self.session.bridge.call.return_value['xml'] = FOUR.read_text()
        self.snap = board()
        self.assertEqual(S.check_layer_stack(out['plan_file'])['status'], 'fail')

    def test_extracts_only_stack_from_edru(self):
        source = STACKS.parent / 'rules' / (FOUR.stem + '.edru')
        out = self.prepare(stackup_file=str(source))
        root = ET.parse(out['file']).getroot()
        self.assertIsNone(root.find('.//param'))
        self.assertIsNone(root.find('.//rule'))
        self.assertEqual(ST.validate_stackup(root), ST.validate_stackup(FOUR.read_bytes()))

    def test_stale_working_copy_wrong_board_and_changed_file_cannot_pass(self):
        out = self.prepare(); self.snap = board((1, 2, 15, 16))
        self.doc['modified'] = True
        self.assertEqual(S.check_layer_stack(out['plan_file'])['status'], 'unverified')
        self.doc['modified'] = False; self.doc['data_file_id'] = 'wrong'
        with self.assertRaisesRegex(Exception, 'board'):
            S.check_layer_stack(out['plan_file'])
        self.doc['data_file_id'] = 'file1'
        Path(out['file']).write_text(FOUR.read_text() + '\n')
        with self.assertRaisesRegex(Exception, 'changed'):
            S.check_layer_stack(out['plan_file'])


if __name__ == '__main__':
    unittest.main()
