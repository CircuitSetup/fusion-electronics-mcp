import copy
import hashlib
import json
import re
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from fusion_mcp.session import Snapshot

SOURCE = b'output(MCP_OUTPUT, "wt") { printf("result"); } exit(0);'
XML = (Path(__file__).parent / 'fixtures/mini.brd').read_bytes()
CHANGE = {'document': 'board', 'xpath': "./drawing/board/elements/element[@name='R1']", 'attributes': {'x': '6.0000'}}


class CustomUlpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def modules(self):
        from fusion_mcp.addin.FusionElectronicsMCP import ulp_support
        from fusion_mcp import custom_ulp
        return ulp_support, custom_ulp

    def register(self, mode='edit'):
        U, _ = self.modules()
        source = self.root / 'source.ulp'
        source.write_bytes(SOURCE)
        return U.register(self.root / 'registry', 'example', source, mode, ['board'], 0,
                          hashlib.sha256(SOURCE).hexdigest())

    def test_registration_pins_reviewed_bytes_and_rejects_changes(self):
        U, _ = self.modules()
        entry = self.register()
        (self.root / 'source.ulp').write_text('system("oops");')
        self.assertEqual(U.load(self.root / 'registry', 'example')[1], SOURCE)
        (self.root / 'registry' / (entry['sha256'] + '.ulp')).write_bytes(b'tampered')
        with self.assertRaises(ValueError): U.load(self.root / 'registry', 'example')

    def test_rejects_wrong_review_hash_names_and_unsafe_source(self):
        U, _ = self.modules()
        source = self.root / 'source.ulp'; source.write_bytes(SOURCE)
        for name, digest in [('../escape', hashlib.sha256(SOURCE).hexdigest()), ('example', '0' * 64)]:
            with self.assertRaises(ValueError): U.register(self.root, name, source, 'read', ['board'], 0, digest)
        for code in ['system("x");', 'exit("MOVE R1 (0 0)");', 'exit(commands);',
                     '#include "other.ulp"', '#define EXEC system', 'netpost("url", "x");',
                     'output(argv[1], "wt") {}', 'output("outside", "wt") {}',
                     'setgroup(E);', 'dlgMessageBox("x");', 'fileread(s,"secret");']:
            with self.subTest(code=code), self.assertRaises(ValueError): U.check_source((code + '\n' + SOURCE.decode()).encode())
        U.check_source(b'// system("not code");\n' + SOURCE)

    def test_arguments_cannot_add_commands(self):
        U, _ = self.modules()
        entry = self.register(); entry['argument_count'] = 1
        self.assertEqual(U.arguments(entry, ['R1']), ['R1'])
        for args in [[], ['x', 'y'], [1], ["x'; RUN evil;"], ['x\nRUN evil'], ['x"']]:
            with self.subTest(args=args), self.assertRaises(ValueError): U.arguments(entry, args)

    def test_preview_apply_verifies_whole_design_and_rejects_replay(self):
        U, M = self.modules(); entry = self.register()
        session = FakeSession(entry)
        result = M.preview(session, 'example', [], 'session:one', self.root / 'registry', self.root / 'plans')
        self.assertEqual(session.writes, [])
        self.assertIn('MOVE', Path(result['preview_path']).read_text())
        applied = M.apply(session, result['plan_path'], result['sha256'], self.root / 'registry', self.root / 'plans')
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(len(session.writes), 1)
        self.assertEqual(session.writes[0]['expected_document_id'], 'session:one')
        with self.assertRaises(ValueError): M.apply(session, result['plan_path'], result['sha256'], self.root / 'registry', self.root / 'plans')

    def test_stale_altered_or_unregistered_plans_do_not_write(self):
        U, M = self.modules(); entry = self.register()
        for condition in ('tab', 'xml', 'plan', 'registry'):
            session = FakeSession(entry)
            directory = self.root / condition
            result = M.preview(session, 'example', [], 'session:one', self.root / 'registry', directory)
            if condition == 'tab': session.doc = 'session:other'
            if condition == 'xml': session.xml = XML.replace(b'value="10k"', b'value="20k"', 1)
            if condition == 'plan':
                path = Path(result['plan_path']); path.write_text(path.read_text().replace('MOVE', 'DELETE'))
            if condition == 'registry': (self.root / 'registry/example.json').unlink()
            with self.subTest(condition=condition), self.assertRaises((ValueError, FileNotFoundError)):
                M.apply(session, result['plan_path'], result['sha256'], self.root / 'registry', directory)
            self.assertEqual(session.writes, [])

    def test_new_preview_after_failed_attempt_gets_a_new_single_use_hash(self):
        _, M = self.modules(); session = FakeSession(self.register())
        registry, plans = self.root / 'registry', self.root / 'plans'
        first = M.preview(session, 'example', [], 'session:one', registry, plans)
        session.outcome = 'timeout'
        self.assertEqual(M.apply(session, first['plan_path'], first['sha256'], registry, plans)['status'], 'state_unknown')
        session.outcome = 'ok'
        fresh = M.preview(session, 'example', [], 'session:one', registry, plans)
        self.assertNotEqual(first['sha256'], fresh['sha256'])
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            M.apply(session, first['plan_path'], first['sha256'], registry, plans)
        self.assertEqual(M.apply(session, fresh['plan_path'], fresh['sha256'], registry, plans)['status'], 'applied')
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            M.apply(session, fresh['plan_path'], fresh['sha256'], registry, plans)

    def test_old_or_malformed_preview_identity_cannot_be_applied(self):
        U, M = self.modules(); session = FakeSession(self.register())
        registry, plans = self.root / 'registry', self.root / 'plans'
        preview = M.preview(session, 'example', [], 'session:one', registry, plans)
        original = json.loads(Path(preview['plan_path']).read_text())
        for change in ({'schema': 1}, {'preview_id': None}, {'preview_id': 'not-a-preview-id'}):
            plan = original | change; del plan['sha256']
            sha = U.digest(plan); plan['sha256'] = sha; path = plans / (sha + '.json'); U.atomic_json(path, plan)
            with self.assertRaisesRegex(ValueError, 'new preview'): M.apply(session, path, sha, registry, plans)
            self.assertEqual(session.writes, [])

    def schematic_session(self):
        U, _ = self.modules(); entry = self.register()
        entry['editors'] = ['schematic']; U.atomic_json(self.root / 'registry/example.json', entry)
        return SchematicSession(entry)

    def test_schematic_apply_selects_reviewed_sheet_after_user_switches(self):
        _, M = self.modules(); session = self.schematic_session()
        registry, plans = self.root / 'registry', self.root / 'plans'
        plan = M.preview(session, 'example', [], 'session:one', registry, plans)
        self.assertIn('EDIT .s1;', plan['commands'])
        session.sheet = 2
        self.assertEqual(M.apply(session, plan['plan_path'], plan['sha256'], registry, plans)['status'], 'applied')
        wires = ET.fromstring(session.sch).findall('./drawing/schematic/sheets/sheet/plain/wire')
        self.assertEqual([w.get('x1') for w in wires], ['2', '0'])

    def test_schematic_preview_rejects_missing_or_unsafe_sheet_selectors(self):
        _, M = self.modules(); session = self.schematic_session()
        for commands in ('MOVE (0 0) (2 0);', 'MOVE (0 0) (2 0); EDIT .s1;',
                         'EDIT .s3; MOVE (0 0) (2 0);', 'EDIT .s0; MOVE (0 0) (2 0);',
                         'EDIT other.sch; MOVE (0 0) (2 0);',
                         'EDIT .s1; MOVE (0 0) (2 0); EDIT .s3;'):
            session.proposal['commands'] = commands
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                M.preview(session, 'example', [], 'session:one', self.root / 'registry', self.root / 'plans')
            self.assertEqual(session.writes, [])

    def test_apply_revalidates_sheet_selectors_in_rehashed_plan(self):
        U, M = self.modules(); session = self.schematic_session()
        registry, plans = self.root / 'registry', self.root / 'plans'
        preview = M.preview(session, 'example', [], 'session:one', registry, plans)
        original = json.loads(Path(preview['plan_path']).read_text())
        for commands in ('GRID MM 0.0001; MOVE (0 0) (2 0); GRID LAST;',
                         original['commands'].replace('EDIT .s1', 'EDIT .s3'),
                         original['commands'].replace('EDIT .s1', 'EDIT other.sch')):
            plan = dict(original, commands=commands); del plan['sha256']
            sha = U.digest(plan); plan['sha256'] = sha; path = plans / (sha + '.json'); U.atomic_json(path, plan)
            with self.assertRaises(ValueError): M.apply(session, path, sha, registry, plans)
            self.assertEqual(session.writes, [])

    def test_bad_readback_timeout_and_unexpected_dialog_consume_attempt(self):
        U, M = self.modules(); entry = self.register()
        for outcome in ('wrong', 'timeout', 'dialog', 'export_failed', 'unrelated'):
            session = FakeSession(entry); session.outcome = outcome
            directory = self.root / outcome
            result = M.preview(session, 'example', [], 'session:one', self.root / 'registry', directory)
            applied = M.apply(session, result['plan_path'], result['sha256'], self.root / 'registry', directory)
            self.assertEqual(applied['status'], 'state_unknown', outcome)
            self.assertEqual(len(session.writes), 1)
            with self.assertRaises(ValueError): M.apply(session, result['plan_path'], result['sha256'], self.root / 'registry', directory)
            self.assertEqual(len(session.writes), 1)

    def test_rehashed_plan_cannot_bypass_command_validation(self):
        U, M = self.modules(); entry = self.register(); session = FakeSession(entry)
        plans = self.root / 'plans'
        result = M.preview(session, 'example', [], 'session:one', self.root / 'registry', plans)
        plan = json.loads(Path(result['plan_path']).read_text())
        plan['commands'] = 'GRID MM 0.0001; RUN evil; GRID LAST;'
        del plan['sha256']; sha = U.digest(plan); plan['sha256'] = sha
        path = plans / (sha + '.json'); U.atomic_json(path, plan)
        with self.assertRaises(ValueError): M.apply(session, path, sha, self.root / 'registry', plans)
        self.assertEqual(session.writes, [])

    def test_commands_and_postconditions_are_required_and_checked(self):
        _, M = self.modules(); entry = self.register()
        for commands in ('RUN evil;', 'SET CONTEXT Element x \'RUN evil\';', 'WRITE;', 'UNDO;', 'EDIT .s1; MOVE R1 (1 2);',
                         "MOVE 'R1' (1 2)\nRUN evil;", "MOVE 'R1 (1 2);", 'GRID MM;', ''):
            session = FakeSession(entry); session.proposal['commands'] = commands
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                M.preview(session, 'example', [], 'session:one', self.root / 'registry', self.root / 'plans')
            self.assertEqual(session.writes, [])
        for changes in ([], [dict(CHANGE, xpath='.//element')], [dict(CHANGE, attributes={'x': 'NaN'})],
                        [CHANGE, CHANGE], [dict(CHANGE, document='schematic')]):
            session = FakeSession(entry); session.proposal['changes'] = changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                M.preview(session, 'example', [], 'session:one', self.root / 'registry', self.root / 'plans')

    def test_preview_detects_generator_mutations(self):
        _, M = self.modules(); entry = self.register()
        session = FakeSession(entry); session.mutate_generator = True
        with self.assertRaisesRegex(ValueError, 'changed'):
            M.preview(session, 'example', [], 'session:one', self.root / 'registry', self.root / 'plans')
        self.assertEqual(session.writes, [])

    def test_fusion_cache_discovery_reports_reviewable_sources(self):
        _, M = self.modules()
        cache = self.root / 'account/AssetCache/ulpfilesCache'; cache.mkdir(parents=True)
        (cache / 'existing(123-1).ulp').write_bytes(b'exit("MOVE R1 (0 0)");')
        result = M.discover('existing', root=self.root)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['results'][0]['sha256'], hashlib.sha256(b'exit("MOVE R1 (0 0)");').hexdigest())

    def test_legacy_requires_explicit_hash_and_records_diff_without_claiming_verified(self):
        U, M = self.modules()
        source = self.root / 'legacy.ulp'; source.write_bytes(b'exit("MOVE R1 (6 5)");')
        entry = U.register(self.root / 'registry', 'example', source, 'legacy', ['board'], 0,
                           hashlib.sha256(source.read_bytes()).hexdigest())
        session = FakeSession(entry)
        with self.assertRaises(ValueError):
            M.legacy(session, 'example', [], 'session:one', '0'*64, self.root / 'registry', self.root / 'runs')
        self.assertEqual(session.writes, [])
        result = M.legacy(session, 'example', [], 'session:one', entry['sha256'], self.root / 'registry', self.root / 'runs')
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual(result['diff']['board']['attributes_changed'], 1)
        self.assertTrue(Path(result['report_path']).is_file())

    def test_legacy_unknown_state_keeps_before_snapshot_and_does_not_retry(self):
        U, M = self.modules(); source = self.root / 'legacy.ulp'; source.write_bytes(b'exit(0);')
        entry = U.register(self.root / 'registry', 'example', source, 'legacy', ['board'], 0,
                           hashlib.sha256(source.read_bytes()).hexdigest())
        session = FakeSession(entry); session.outcome = 'timeout'
        result = M.legacy(session, 'example', [], 'session:one', entry['sha256'], self.root / 'registry', self.root / 'runs')
        self.assertEqual(result['status'], 'state_unknown')
        self.assertIn('before', json.loads(Path(result['report_path']).read_text()))

    def test_legacy_preserves_bridge_result_before_failed_readback(self):
        _, M = self.modules(); entry = self.register('legacy')
        for failure in ('export_failed', 'version_changed'):
            session = FakeSession(entry); session.outcome = failure
            result = M.legacy(session, 'example', [], 'session:one', entry['sha256'],
                              self.root / 'registry', self.root / 'runs')
            self.assertEqual(result['status'], 'state_unknown')
            expected = {'legacy': True, 'artifact_directory': 'retained-output', 'raw': 'completed'}
            self.assertEqual(result.get('bridge_result'), expected)
            report = json.loads(Path(result['report_path']).read_text())
            self.assertEqual(report['bridge_result'], expected)
            self.assertEqual(session.report_at_export['bridge_result'], expected)


class FakeSession:
    def __init__(self, entry):
        self.bridge = self; self.lock = self; self.xml = XML; self.doc = 'session:one'
        self.entry = entry; self.writes = []; self.outcome = 'ok'; self.mutate_generator = False
        self.proposal = {'schema': 1, 'name': 'example', 'mode': 'edit', 'editor': 'board', 'complete': True,
                         'commands': "MOVE 'R1' (6 5);", 'changes': [copy.deepcopy(CHANGE)]}
    def transport(self): return 'addin'
    def hold(self, *a): return nullcontext()
    def context(self):
        return {'active_editor': 'board', 'active_document': {'name': 'PCB', 'kind': 'board',
                'document_id': self.doc, 'project_id': 'one', 'modified': False,
                'version': 2 if self.writes and self.outcome == 'version_changed' else 1}}
    def snapshot(self):
        if self.entry['mode'] == 'legacy' and self.writes:
            reports = Path(self.entry['source_path']).parent / 'runs'
            self.report_at_export = json.loads(max(reports.glob('*.json'), key=lambda p: p.stat().st_mtime_ns).read_text())
        if self.writes and self.outcome == 'export_failed': raise OSError('export failed')
        return Snapshot(board_xml=self.xml)
    def call(self, op, args, **kwargs):
        if op == 'run_registered_ulp':
            if self.entry['mode'] == 'legacy':
                self.writes.append(args)
                if self.outcome == 'timeout': raise TimeoutError('uncertain')
                self.xml = XML.replace(b'x="5"', b'x="6"', 1)
                return {'legacy': True, 'artifact_directory': 'retained-output', 'raw': 'completed'}
            if self.mutate_generator: self.xml = XML.replace(b'x="5"', b'x="8"', 1)
            return {'data': copy.deepcopy(self.proposal)}
        assert op == 'run', op
        self.writes.append(args)
        if self.outcome == 'timeout': raise TimeoutError('connection lost')
        root = ET.fromstring(self.xml)
        root.find(CHANGE['xpath']).set('x', '7' if self.outcome == 'wrong' else '6')
        if self.outcome == 'unrelated': root.find(".//element[@name='R2']").set('value', 'wrong')
        self.xml = ET.tostring(root)
        return {'dialogs': [{'expected': False}]} if self.outcome == 'dialog' else {}


class SchematicSession(FakeSession):
    def __init__(self, entry):
        super().__init__(entry)
        from test_server import load_addin
        self.addin = load_addin()
        self.sheet = 1
        sheet = '<sheet><plain><wire x1="0" y1="0" x2="1" y2="1"/></plain></sheet>'
        self.sch = ('<eagle><drawing><schematic><sheets>' + sheet * 2 + '</sheets></schematic></drawing></eagle>').encode()
        self.proposal.update(editor='schematic', commands='EDIT .s1; MOVE (0 0) (2 0);', changes=[{
            'document': 'schematic', 'xpath': './drawing/schematic/sheets/sheet[1]/plain/wire', 'attributes': {'x1': '2'}}])

    def context(self):
        result = super().context(); result['active_editor'] = 'schematic'; result['active_document']['kind'] = 'schematic'
        return result

    def snapshot(self): return Snapshot(sch_xml=self.sch)

    def call(self, op, args, **kwargs):
        if op == 'run_registered_ulp': return {'data': copy.deepcopy(self.proposal)}
        self.writes.append(args)
        for command in self.addin.validate(args['commands']):
            if match := re.fullmatch(r'EDIT \.s([1-9][0-9]*)', command): self.sheet = int(match[1])
            if command.startswith('MOVE '):
                root = ET.fromstring(self.sch)
                root.find(f'./drawing/schematic/sheets/sheet[{self.sheet}]/plain/wire').set('x1', '2')
                self.sch = ET.tostring(root)
        return {}


if __name__ == '__main__': unittest.main()
