import contextlib
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fusion_mcp import eagle_pair_picker as picker
from test_eagle_pair_import import SCH, BRD
from fusion_mcp.addin.FusionElectronicsMCP.management import EventLog


class Native:
    def __init__(self):
        self.identity = [10, 11, 12]
        self.posts = []
        self.bad = False
        self.fail_post = False
    def find(self, pid, extension):
        if self.bad:
            raise ValueError('Foreign window')
        return self.identity
    def set_path(self, identity, path): self.path = path
    def post(self, identity):
        self.posts.append(identity[:])
        if self.fail_post: raise OSError('uncertain post')


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eagle-pair-', dir=self.stage_root())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        self.hashes = {}
        for kind, suffix, text in [('schematic', '.sch', SCH), ('board', '.brd', BRD)]:
            path = self.root / ('Pair' + suffix)
            path.write_text(text)
            self.paths[kind] = str(path)
            self.hashes[kind + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.ticket = dict(request_id='a'*32, session_id='session', status='awaiting_file_selection',
                           dispatch_acknowledged=True, staged=self.paths,
                           expected=dict(physical_parts=1, schematic_parts=1, sheets=1, contacts=2),
                           destination=dict(project_id='p', folder_id='f', name='Product'))
        self.info = dict(pid=123, token='secret', event_session_id='session', addin_version='0.14.21')
        self.events = EventLog()
        self.events.session_id = 'session'
        self.events.supported = ['commandStarting', 'commandTerminated']
        self.calls = []
        self.start_error = False
        def call(op, args, **kwargs):
            self.calls.append((op, dict(args)))
            if op == 'events': return self.events.read(**args)
            if op == 'start_eagle_pair_import':
                if self.start_error: raise OSError('uncertain start')
                if not self.events.sequence:
                    self.events.append('commandStarting', command_id='ImportSCHAndBRDCmd')
            return self.ticket
        self.bridge = types.SimpleNamespace(lock=types.SimpleNamespace(hold=lambda _: contextlib.nullcontext()),
                                            _info=lambda: dict(self.info),
                                            call=call)
        self.native = Native()
        self.addCleanup(patch.stopall)
        patch.object(picker, 'data_dir', lambda name: str(self.root/name)).start()
        self.start_args = dict(request_id='a'*32, schematic_path=self.paths['schematic'],
                               board_path=self.paths['board'], **self.hashes,
                               project_id='p', name='Product', folder='')
        picker.begin(self.bridge, self.start_args)

    @staticmethod
    def stage_root():
        root = Path(tempfile.gettempdir()) / 'fusion-electronics-mcp'
        root.mkdir(exist_ok=True)
        return root

    def select(self, kind='schematic', **changes):
        args = dict(bridge=self.bridge, request_id='a'*32, kind=kind, expected_session_id='session',
                    **self.hashes, project_id='p', folder_id='f', name='Product',
                    native=self.native, receipt_dir=self.root/'receipts')
        args.update(changes)
        return picker.select_file(**args)

    def test_submit_once_and_board_requires_transition(self):
        self.assertEqual(self.select()['status'], 'submitted')
        receipt = json.loads((self.root/'receipts'/('a'*32 + '.json')).read_text())
        self.assertEqual(receipt['schematic']['status'], 'posted')
        with self.assertRaises(ValueError): self.select()
        self.assertEqual(self.select('board')['status'], 'pending')
        self.native.identity = [20, 21, 22]
        self.assertEqual(self.select('board')['status'], 'submitted')
        self.assertEqual(len(self.native.posts), 2)

    def test_closed_previous_picker_allows_reused_identity(self):
        self.select()
        self.native.identity = None
        self.assertEqual(self.select('board')['status'], 'pending')
        self.native.identity = [10, 11, 12]
        self.assertEqual(self.select('board')['status'], 'submitted')

    def test_uncertain_post_consumes_attempt(self):
        self.native.fail_post = True
        with self.assertRaises(OSError): self.select()
        with self.assertRaises(ValueError): self.select()
        self.assertEqual(len(self.native.posts), 1)
        self.native.fail_post = False
        self.native.identity = [20, 21, 22]
        with self.assertRaises(ValueError): self.select('board')
        self.assertEqual(len(self.native.posts), 1)

    def test_boundaries_reject_before_post(self):
        for changes in [dict(kind='other'), dict(project_id='wrong'), dict(folder_id='wrong'),
                        dict(name='wrong'), dict(expected_session_id='other'),
                        dict(schematic_sha256='0'*64), dict(board_sha256='0'*64)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.select(**changes)
        for key, value in [('save_requested', True), ('status', 'queued'), ('dispatch_acknowledged', False)]:
            old = self.ticket.get(key)
            self.ticket[key] = value
            with self.assertRaises(ValueError): self.select()
            self.ticket[key] = old
        self.native.bad = True
        with self.assertRaises(ValueError): self.select()
        self.assertEqual(self.native.posts, [])

    def test_board_cannot_be_first(self):
        with self.assertRaises(ValueError): self.select('board')

    def test_untrusted_staging_and_summary(self):
        self.ticket['expected']['sheets'] = 9
        with self.assertRaises(ValueError): self.select()
        self.ticket['expected']['sheets'] = 1
        self.ticket['staged'] = dict(self.paths, schematic=str(self.root.parent/'Pair.sch'))
        with self.assertRaises(ValueError): self.select()

    def test_session_rotation_before_post(self):
        def change_session(identity, path): self.info['token'] = 'rotated'
        self.native.set_path = change_session
        with self.assertRaises(ValueError): self.select()
        self.assertEqual(self.native.posts, [])

    def test_ticket_changes_before_post(self):
        import copy
        original = copy.deepcopy(self.ticket)
        mutations = [lambda t: t.update(request_id='b'*32), lambda t: t.update(session_id='new'),
                     lambda t: t.update(status='state_unknown'), lambda t: t.update(dispatch_acknowledged=False),
                     lambda t: t.update(save_requested=True),
                     lambda t: t['destination'].update(name='Changed'),
                     lambda t: t['staged'].update(board='different.brd'),
                     lambda t: t['expected'].update(sheets=9)]
        for mutate in mutations:
            self.ticket = copy.deepcopy(original)
            self.native.set_path = lambda identity, path: mutate(self.ticket)
            with self.subTest(mutation=mutations.index(mutate)), self.assertRaises(ValueError):
                self.select()
        self.assertEqual(self.native.posts, [])

    def test_begin_retry_preserves_cursor_and_exact_binding(self):
        journal = self.root/'import-requests'/('a'*32+'.json')
        original = json.loads(journal.read_text())
        picker.begin(self.bridge, self.start_args)
        self.assertEqual(json.loads(journal.read_text())['after_seq'], original['after_seq'])
        for key, value in [('name', 'different'), ('schematic_sha256', '0'*64)]:
            with self.assertRaises(ValueError): picker.begin(self.bridge, dict(self.start_args, **{key: value}))
        self.info['token'] = 'rotated'
        with self.assertRaises(ValueError): picker.begin(self.bridge, self.start_args)

    def reload_not_started(self):
        self.ticket.update(status='not_started', dispatch_acknowledged=False)
        self.info.update(event_session_id='reloaded', token='new-token')
        self.events = EventLog()
        self.events.session_id = 'reloaded'
        self.events.supported = ['commandStarting', 'commandTerminated']
        self.events.append('documentOpened')
        original_call = self.bridge.call

        def call(op, args, **kwargs):
            result = original_call(op, args, **kwargs)
            if op == 'start_eagle_pair_import':
                self.ticket.update(session_id='reloaded', status='awaiting_file_selection',
                                   dispatch_acknowledged=True)
                self.events.append('commandStarting', command_id='ImportSCHAndBRDCmd')
            return result

        self.bridge.call = call

    def test_begin_rebinds_retained_not_started_ticket_after_reload(self):
        self.reload_not_started()
        result = picker.begin(self.bridge, self.start_args)
        self.assertEqual(result['session_id'], 'reloaded')
        journal = json.loads((self.root/'import-requests'/('a'*32+'.json')).read_text())
        self.assertEqual(journal['after_seq'], 1)
        self.assertEqual(journal['binding']['session_id'], 'reloaded')
        self.assertEqual(self.select(expected_session_id='reloaded')['status'], 'submitted')

    def test_begin_reload_rejects_changed_args_without_native_start(self):
        self.reload_not_started()
        count = len(self.calls)
        for key, value in [('name', 'Changed'), ('folder', 'other'),
                           ('schematic_path', 'different.sch'), ('board_sha256', '0'*64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                picker.begin(self.bridge, dict(self.start_args, **{key: value}))
        self.assertEqual(len(self.calls), count)

    def test_begin_rebinds_acknowledged_dispatch_that_never_launched(self):
        self.reload_not_started()
        self.ticket['dispatch_acknowledged'] = True
        self.assertEqual(picker.begin(self.bridge, self.start_args)['session_id'], 'reloaded')
        self.assertEqual(self.select(expected_session_id='reloaded')['status'], 'submitted')

    def test_begin_reload_requires_original_known_not_started_ticket(self):
        self.reload_not_started()
        journal_path = self.root/'import-requests'/('a'*32+'.json')
        original = journal_path.read_text()
        for changes in [dict(status='queued'), dict(status='state_unknown'),
                        dict(status='awaiting_file_selection'), dict(session_id='unrelated'),
                        dict(request_id='b'*32),
                        dict(save_requested=True), dict(destination=dict(self.ticket['destination'], folder_id='other')),
                        dict(expected=dict(self.ticket['expected'], sheets=9))]:
            saved = dict(self.ticket)
            self.ticket.update(changes)
            count = len(self.calls)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                picker.begin(self.bridge, self.start_args)
            self.assertNotIn('start_eagle_pair_import', [op for op, _ in self.calls[count:]])
            self.assertEqual(journal_path.read_text(), original)
            self.ticket = saved

    def test_begin_reload_refuses_unknown_ticket_and_changed_source(self):
        self.reload_not_started()
        call = self.bridge.call
        def unknown(op, args, **kwargs):
            if op == 'eagle_pair_import_status': raise ValueError('Unknown import request')
            return call(op, args, **kwargs)
        self.bridge.call = unknown
        with self.assertRaises(ValueError): picker.begin(self.bridge, self.start_args)
        self.bridge.call = call
        Path(self.paths['board']).write_text(BRD + '\n')
        count = len(self.calls)
        with self.assertRaises(ValueError): picker.begin(self.bridge, self.start_args)
        self.assertNotIn('start_eagle_pair_import', [op for op, _ in self.calls[count:]])

    def test_begin_reload_refuses_session_change_before_start(self):
        self.reload_not_started()
        call = self.bridge.call
        def rotate(op, args, **kwargs):
            result = call(op, args, **kwargs)
            if op == 'events': self.info['token'] = 'rotated-again'
            return result
        self.bridge.call = rotate
        count = len(self.calls)
        with self.assertRaises(ValueError): picker.begin(self.bridge, self.start_args)
        self.assertNotIn('start_eagle_pair_import', [op for op, _ in self.calls[count:]])

    def test_begin_reload_requires_same_process_and_new_event_session(self):
        self.reload_not_started()
        for changes in [dict(pid=456), dict(event_session_id='session')]:
            saved = dict(self.info)
            self.info.update(changes)
            count = len(self.calls)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                picker.begin(self.bridge, self.start_args)
            self.assertNotIn('start_eagle_pair_import', [op for op, _ in self.calls[count:]])
            self.info = saved

    def test_untracked_ticket_and_hash_rebinding_refused(self):
        # A different valid pair with the same summary must not replace the original pins.
        path = Path(self.paths['schematic'])
        path.write_text(SCH.replace('<eagle>', '<eagle version="9">'))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): self.select(schematic_sha256=digest)
        journal = self.root/'import-requests'/('a'*32+'.json')
        journal.unlink()
        with self.assertRaises(ValueError): self.select()

    def test_command_termination_blocks_filename_and_post(self):
        self.events.append('commandTerminated', command_id='ImportSCHAndBRDCmd')
        with self.assertRaises(ValueError): self.select()
        self.assertFalse(hasattr(self.native, 'path'))
        self.assertEqual(self.native.posts, [])

    def test_termination_during_filename_set_blocks_post(self):
        self.native.set_path = lambda *args: self.events.append('commandTerminated', command_id='ImportSCHAndBRDCmd')
        with self.assertRaises(ValueError): self.select()
        self.assertEqual(self.native.posts, [])

    def test_missing_or_duplicate_start_and_truncated_events(self):
        self.events.events.clear()
        self.events.sequence = 0
        with self.assertRaises(ValueError): self.select()
        self.events.append('commandStarting', command_id='ImportSCHAndBRDCmd')
        self.events.append('commandStarting', command_id='ImportSCHAndBRDCmd')
        with self.assertRaises(ValueError): self.select()
        self.events.events.clear()
        self.events.sequence = 0
        self.events.append('commandStarting', command_id='ImportSCHAndBRDCmd')
        for _ in range(200): self.events.append('commandStarting', command_id='Other')
        with self.assertRaises(ValueError): self.select()
        self.assertTrue(all(args['limit'] <= 200 for op, args in self.calls if op == 'events'))

    def test_reset_and_dropped_event_history_rejected(self):
        self.events.session_id = 'new-session'
        with self.assertRaises(ValueError): self.select()
        self.events.session_id = 'session'
        for _ in range(501): self.events.append('documentOpened')
        with self.assertRaises(ValueError): self.select()
        self.assertFalse(hasattr(self.native, 'path'))

    def test_begin_rejects_invalid_hash_and_version_without_start(self):
        count = len(self.calls)
        with self.assertRaises(ValueError):
            picker.begin(self.bridge, dict(self.start_args, request_id='c'*32, board_sha256='x'*64))
        self.info['addin_version'] = '0.14.20'
        with self.assertRaises(ValueError): picker.begin(self.bridge, dict(self.start_args, request_id='c'*32))
        self.assertEqual(len(self.calls), count)

    def test_prepared_uncertain_start_can_repeat_same_args(self):
        path = self.root/'import-requests'/('b'*32+'.json')
        args = dict(self.start_args, request_id='b'*32)
        self.start_error = True
        with self.assertRaises(OSError): picker.begin(self.bridge, args)
        prepared = json.loads(path.read_text())
        self.assertNotIn('destination', prepared)
        self.start_error = False
        self.ticket['request_id'] = 'b'*32
        picker.begin(self.bridge, args)
        self.assertEqual(json.loads(path.read_text())['after_seq'], prepared['after_seq'])


class WindowsAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = picker.WindowsPicker.__new__(picker.WindowsPicker)
        self.windows = {
            10: dict(pid=123, cls='#32770', text='Open', owner=9, visible=True, enabled=True, number=0),
            9: dict(pid=123, cls='Main', text='Fusion', owner=0, visible=True, enabled=False, number=0),
            11: dict(pid=123, cls='Edit', text='', owner=0, visible=True, enabled=True, number=1148),
            12: dict(pid=123, cls='Button', text='&Open', owner=0, visible=True, enabled=True, number=1),
            13: dict(pid=123, cls='ComboBox', text='Eagle files (*.sch;*.brd)', owner=0, visible=True, enabled=True, number=1136),
        }
        self.adapter.enumerate = lambda parent=None: [10, 9] if parent is None else [11, 12, 13]
        self.adapter.pid = lambda h: self.windows[h]['pid']
        self.adapter.cls = lambda h: self.windows[h]['cls']
        self.adapter.text = lambda h: self.windows[h]['text']
        self.adapter.u = types.SimpleNamespace(
            IsWindowVisible=lambda h: self.windows[h]['visible'],
            IsWindowEnabled=lambda h: self.windows[h]['enabled'],
            GetWindow=lambda h, _: self.windows[h]['owner'],
            GetDlgCtrlID=lambda h: self.windows[h]['number'])

    def test_exact_owned_picker(self):
        self.assertEqual(self.adapter.find(123, '.sch'), [10, 11, 12])

    def test_foreign_owner_and_controls_rejected(self):
        for hwnd, key, value in [(9, 'pid', 999), (11, 'pid', 999), (11, 'enabled', False),
                                 (11, 'number', 100), (12, 'text', 'Save'),
                                 (13, 'text', '*.txt'), (10, 'owner', 0), (9, 'owner', 10)]:
            old = self.windows[hwnd][key]
            self.windows[hwnd][key] = value
            with self.subTest(hwnd=hwnd, key=key), self.assertRaises(ValueError):
                self.adapter.find(123, '.sch')
            self.windows[hwnd][key] = old

    def test_duplicate_picker_rejected(self):
        self.windows[20] = dict(self.windows[10])
        self.adapter.enumerate = lambda parent=None: [10, 20] if parent is None else [11, 12, 13]
        with self.assertRaises(ValueError): self.adapter.find(123, '.sch')

    def test_owned_error_is_not_closed_picker(self):
        self.windows[10]['text'] = 'Error'
        with self.assertRaises(ValueError): self.adapter.find(123, '.sch')
