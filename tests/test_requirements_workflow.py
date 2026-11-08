import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fusion_mcp import design_requirements as DR, server as S, design_store
from fusion_mcp.session import Session, WriteFailed
from fusion_offline import requirements as R
from test_design_requirements import fixture, req
from test_move_parts import FakeFusion


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.spec, self.evidence = fixture()
        self.spec['requirements'] = [req('distance', pads=['U1.VIN', 'C1.1'], net='VIN', max_mm=3)]
        for source in self.spec['sources']:
            self.spec['sources'][source]['manifest_path'] = source
        self.path = Path(self.tmp.name) / 'requirements.json'
        self.path.write_text(json.dumps(self.spec))
        self.fake = FakeFusion(self.root)
        original = self.fake.call
        def call(op, *a, **kw):
            if op == 'context': return {'active_document': {'name': 'Test', 'kind': 'board', 'document_id': 'doc1', 'data_file_id': 'file1'}}
            return original(op, *a, **kw)
        self.fake.call = call
        self.session = Session(self.fake)
        self.store = {}
        def update(key, fn):
            data = self.store.setdefault(key, {})
            fn(data)
            return data, 'store.json'
        for target, replacement in [(S, {'session': self.session, '_move_picture': lambda *a: None}),
                                    (design_store, {'load': lambda key: self.store.get(key, {}), 'update': update}),
                                    (DR.datasheets, {'load': lambda path: self.evidence[Path(path).name]})]:
            for name, val in replacement.items():
                p = patch.object(target, name, val); p.start(); self.addCleanup(p.stop)

    def bind(self):
        return DR.bind(self.session, str(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest(), 'doc1')

    def test_binding_exact_document_and_hash(self):
        with self.assertRaisesRegex(ValueError, 'document'):
            DR.bind(self.session, str(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest(), 'other')
        self.bind()
        self.path.write_text(self.path.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'changed'):
            DR.bound(self.session)

    def test_move_preview_reports_violation_and_write_refuses(self):
        self.bind()
        result = json.loads(S._move_parts([{'ref':'C1','x_mm':2}], True)[-1])
        self.assertEqual(result['datasheet_requirements']['status'], 'fail')
        with self.assertRaisesRegex((ValueError, WriteFailed), 'requirement'):
            S._move_parts([{'ref':'C1','x_mm':2}], False)
        self.assertFalse(self.fake.ran)

    def test_readback_regression_undoes_low_level_move(self):
        self.bind()
        before = self.fake.xml
        with self.assertRaisesRegex(WriteFailed, 'requirement'):
            self.session.verified_write('board', "MOVE 'C1' (2 10);", lambda *a: (True, 'commands accepted'), schematic=False)
        self.assertEqual(self.fake.xml, before)

    def test_pin_specific_placement_plan_and_valid_move(self):
        self.bind()
        plan = DR.plan_placement(self.session, self.root)
        self.assertEqual(plan['unresolved'], [])
        result = json.loads(S._move_parts([{'ref':'C1','x_mm':7.5}], False)[-1])
        self.assertTrue(result['written'])
        self.assertNotEqual(result['datasheet_requirements']['status'], 'fail')

    def test_no_constraints_is_explicit(self):
        result = DR.report(self.session, self.root)
        self.assertEqual(result['status'], 'not_supplied')

    def test_same_name_other_identity_has_no_binding(self):
        self.bind()
        with patch.object(self.session, 'context', return_value={'active_document': {'name':'Test','kind':'board','document_id':'doc2','data_file_id':'file2'}}):
            self.assertIsNone(DR.bound(self.session))

    def test_routing_policy_preserves_width_layers_and_rejects_blind_vias(self):
        self.spec['requirements'] = [req('routing', net='VIN', layers=[1,2], min_width_mm=.7,
                                         clearance_mm=.25, min_drill_mm=.4, via_spans=[[1,16]], priority=10)]
        loaded = self.spec, self.evidence, self.path.parent
        width, options = DR.route_options(loaded, 'VIN', .25)
        self.assertEqual(width, .7)
        self.assertEqual(options['layers'], (1,2))
        self.assertEqual(options['clearance'], .25)
        self.assertEqual(options['neck_len'], 0)
        self.spec['requirements'][0]['via_technology'] = 'blind'
        with self.assertRaisesRegex(ValueError, 'Unsupported via'):
            DR.route_options(loaded, 'VIN', .25)

    def test_public_workflow_exports_schema(self):
        result = S.get_datasheet_workflow('TEST-BUCK', 'vendor.example')
        self.assertIn('requirements', result['schema']['properties'])
        self.assertEqual(len(result['research_queries']), 4)
        steps = ' '.join(result['steps'])
        self.assertLess(steps.index('search_fusion_libraries'), steps.index('search the internet'))
        self.assertLess(steps.index('search the internet'), steps.index('create the missing symbol'))
        self.assertIn('download and install/import', steps)
        self.assertIn('3D dimensions, units, origin, rotation, pin-1 and board seating', steps)
        self.assertIn('assets remain incomplete', steps)
        self.assertTrue(all('TEST-BUCK' in q for q in result['library_queries']))

    def test_native_autorouter_refuses_bound_constraints_before_commands(self):
        self.bind()
        with self.assertRaisesRegex(Exception, 'constrained'):
            S.autoroute()
        self.assertFalse(self.fake.ran)

    def test_document_switch_with_stale_binding_never_undoes_other_document(self):
        self.bind()
        self.store['file2'] = {'datasheet_requirements': {'path':'missing.json', 'sha256':'0'*64}}
        original = self.fake.call
        switched = False
        undos = []
        def call(op, args=None, **kw):
            nonlocal switched
            if op == 'context' and switched:
                return {'active_document':{'kind':'board','document_id':'doc2','data_file_id':'file2'}}
            if op == 'run' and 'MOVE' in args['commands']:
                result = original(op, args, **kw)
                switched = True
                return result
            if op == 'run' and 'UNDO' in args['commands']: undos.append('doc2' if switched else 'doc1')
            return original(op, args, **kw)
        self.fake.call = call
        with self.assertRaisesRegex(WriteFailed, 'document changed'):
            self.session.verified_write('board', "MOVE 'C1' (2 10);", lambda *a:(True,'ok'), schematic=False)
        self.assertEqual(undos, [])

    def test_stale_planned_board_is_refused_before_write(self):
        self.bind()
        before = hashlib.sha256(self.fake.xml).hexdigest()
        self.fake.xml += b'<!-- user edit -->'
        with self.assertRaisesRegex(WriteFailed, 'planned board changed'):
            self.session.verified_write('board', "MOVE 'C1' (7.5 10);", lambda *a:(True,'ok'),
                schematic=False, expected_board_sha256=before, expected_document_id='doc1')
        self.assertEqual(self.fake.ran, [])

    def test_placement_candidate_repairs_violating_geometry(self):
        import xml.etree.ElementTree as ET
        self.root.find('.//element[@name="C1"]').set('x','2')
        self.fake.xml = ET.tostring(self.root)
        self.bind()
        plan = DR.plan_placement(self.session, self.root)
        self.assertTrue(plan['moves'])
        self.assertEqual(plan['unresolved'], [])
        self.assertEqual(next(f['status'] for f in plan['report']['findings'] if f['id']=='distance'), 'pass')
