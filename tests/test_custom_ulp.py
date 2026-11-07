import copy
import hashlib
import json
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
        for commands in ('RUN evil;', 'SET CONTEXT Element x \'RUN evil\';', 'WRITE;', 'UNDO;',
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
                'document_id': self.doc, 'project_id': 'one', 'modified': False, 'version': 1}}
    def snapshot(self):
        if self.writes and self.outcome == 'export_failed': raise OSError('export failed')
        return Snapshot(board_xml=self.xml)
    def call(self, op, args, **kwargs):
        if op == 'run_registered_ulp':
            if self.entry['mode'] == 'legacy':
                self.writes.append(args)
                if self.outcome == 'timeout': raise TimeoutError('uncertain')
                self.xml = XML.replace(b'x="5"', b'x="6"', 1)
                return {'legacy': True}
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


if __name__ == '__main__': unittest.main()
